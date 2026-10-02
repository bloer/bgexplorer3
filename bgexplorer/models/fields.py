from mongoengine import (EmbeddedDocument, EmbeddedDocumentListField)
from mongoengine.fields import (BaseField, StringField, BinaryField, IntField,
                                DictField, ObjectIdField, MapField)
import bson
import hashlib
# import pint
import logging
from typing import Union, Optional
import numpy as np
import io
import re
from collections.abc import Mapping

from .asymmetric import AsymmetricUncertainty, LinearExpression
from .arrays import encode_array, decode_array, is_encoded_array
from .histogram import Histogram
from .common import units as unitreg
from .common import pint

log = logging.getLogger(__name__)

UnitType = Union[str, pint.Unit, pint.Quantity]


class InlineAttachment(EmbeddedDocument):
    """ attachments stored as binary blobs within the document """
    id = ObjectIdField(default=bson.ObjectId)
    filename = StringField()
    size = IntField()
    mimetype = StringField()
    description = StringField()
    metadata = DictField()
    data = BinaryField()
    etag = StringField()
    thumbnail = BinaryField()

    def clean(self):
        super().clean()
        if self.data:
            self.size = len(self.data)
            self.etag = hashlib.sha1(self.data).hexdigest()
            # generate thumbnail now or on demand?

def AttachmentsField(**kwargs):
    return EmbeddedDocumentListField(InlineAttachment, **kwargs)


def compress(doc: dict) -> dict:
    """ Encode all numpy arrays in `doc` for storage """
    return {k: encode_array(v) if isinstance(v, np.ndarray) else v
            for k, v in doc.items()}


def decompress(value: Union[Mapping, bytes]) -> dict:
    """ Return a dict of numpy arrays and other values from the output of
    `compress`. Also reads the legacy npz blobs and plain lists
    """
    if isinstance(value, bytes):
        # legacy npz archive
        value = dict(**np.load(io.BytesIO(value)))
        # remove np.array wrapper from non-array items
        for k, v in list(value.items()):
            if v.ndim == 0:
                value[k] = v.item()
        return value
    return {k: decode_array(v) if is_encoded_array(v) else v
            for k, v in value.items()}


def get_fromstr(value) -> Optional[str]:
    """ Return the string `value` was parsed from by `QuantityField`, if any.
    Don't use getattr for this: on a miss, pint's Quantity.__getattr__
    formats the whole magnitude into its error message, which is very slow
    for AsymmetricUncertainty arrays
    """
    try:
        return vars(value).get('_fromstr')
    except TypeError:
        return None


def with_id(value, id: Optional[str]):
    """ `value`, with its AsymmetricUncertainty magnitude given leaf `id` """
    m = getattr(value, 'm', None)
    if id is None or not isinstance(m, AsymmetricUncertainty) or m.id == id:
        return value
    fromstr = get_fromstr(value)
    result = pint.Quantity(AsymmetricUncertainty(m.mode, m.s0, m.s1, id=id),
                           value.u)
    if fromstr:
        result._fromstr = fromstr
    return result


def as_leaf(value):
    """ `value`, with its magnitude a new leaf if it was calculated from
    others. Values stored without their correlations become new variables
    """
    if isinstance(value, AsymmetricUncertainty):
        return (value if value.isleaf else
                AsymmetricUncertainty(value.mode, value.s0, value.s1))
    m = getattr(value, 'm', None)
    if not isinstance(m, AsymmetricUncertainty) or m.isleaf:
        return value
    return pint.Quantity(AsymmetricUncertainty(m.mode, m.s0, m.s1), value.u)


class RawDictField(BaseField):
    """ A dict stored as is. Unlike DictField, bytes in it (e.g. encoded
    arrays) aren't converted to lists
    """
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('default', dict)
        super().__init__(*args, **kwargs)

    def validate(self, value):
        if not isinstance(value, Mapping):
            self.error(f"Value must be a dict, got {type(value)}")


def utostr(unit):
    return '{:~C}'.format(unit)


class UnitField(BaseField):
    """ Store a unit as a string """
    def to_python(self, value):
        if isinstance(value, str):
            try:
                value = unitreg(value)
            except pint.errors.UndefinedUnitError:
                self.error(f"{value} is not a valid unit")
        if hasattr(value, 'u'):
            value = value.u
        return value

    def to_mongo(self, value):
        return utostr(value)

    def validate(self, value):
        if not isinstance(value, pint.Unit):
            self.error(f"{value} is not a pint Unit")

    def prepare_query_value(self, op, value):
        return self.to_mongo(value)

    # make sure values are converted when assigned, not just in constructor
    def __set__(self, instance, value):
        return super().__set__(instance, self.to_python(value))


# regexes to test asymmetric quantities
_refloat = r'([0-9.]+(?:[eE][+-]?\d+)?)'
_limit_test = re.compile(fr'< *{_refloat} *(\([0-9.]+%\))? *(.*)?')
_sym_test = re.compile(fr'\(? *{_refloat} *(?:(?:±|\+ */? *-)'
                       fr' *{_refloat})? *\)? *([eE][+-]?\d+)? *(.*)?')
_asym_test = re.compile(fr'\(? *{_refloat} *\+ *{_refloat} *'
                        fr'- *{_refloat} *\)? *([eE][+-]?\d+)? *(.*)?')
# TODO: handle uncertainties given in parentheses

class QuantityField(BaseField):
    """ A field representing a pint.Quantity.
    Args:
        units: value must have same dimensionality as units
        allownone: if False, raise an error if value is None. If True,
                   allow the value to be None. If an int or float, convert
                   None to that value
        convert: if True, convert provided value to units
        forceasym: if True, force the value to be an AsymmetricUncertainty
        expressions: how to store an AsymmetricUncertainty calculated from
                     others. None: as a new independent variable. 'inline':
                     with the expression it was calculated from, so it's
                     loaded correlated with them. 'separate': marked as
                     calculated, and loaded without its correlations; the
                     document stores the expression elsewhere
    """
    EXPRESSIONS = (None, 'inline', 'separate')

    def __init__(self, *args,
                 units: Optional[UnitType] = None,
                 allownone: Union[bool, int, float] = True,
                 convert: bool = False,
                 forceasym: bool = False,
                 expressions: Optional[str] = None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        if expressions not in self.EXPRESSIONS:
            raise ValueError(f"expressions must be one of {self.EXPRESSIONS}")
        self.expressions = expressions
        if not hasattr(units, 'dimensionality') and units is not None:
            units = unitreg(units)
        self.units = units
        self.allownone = allownone
        self.convert = convert
        if self.units is None:
            self.convert = False
        self.forceasym = forceasym


    def _fromstr(self, value):
        value = value.strip()
        val = limit = quantile = unit = sigma = sigmaup = exponent = None
        if match := _limit_test.fullmatch(value):
            limit, quantile, unit = match.groups()
            limit = float(limit)
            try:
                quantile = float(quantile.strip(' ()%'))/100.
            except AttributeError:
                quantile = 0.9
        elif match := _asym_test.fullmatch(value):
            val, sigmaup, sigma, exponent, unit = match.groups()
        elif match := _sym_test.fullmatch(value):
            val, sigma, exponent, unit = match.groups()
        else:
            raise ValueError(f"'{value}' is not a valid Quantity string")

        exponent = float('1' + exponent) if exponent is not None else 1
        val = float(val) * exponent if val is not None else 1
        sigma = float(sigma) * exponent if sigma is not None else None
        sigmaup = float(sigmaup) * exponent if sigmaup is not None else None

        if limit:
            result = AsymmetricUncertainty.fromlimit(limit, quantile)
        elif sigma is not None:
            result = AsymmetricUncertainty(val, sigma, sigmaup)
        else:
            result = val
        unit = unit or self.units
        result = pint.Quantity(result, unit)
        result._fromstr = value
        return result

    def to_python(self, value):
        units = self.units
        # AsymmetricUncertainties are stored with the id of their variable
        stored_id = None

        if value is None:
            if self.allownone is True:
                return None
            elif self.allownone is False:
                raise ValueError("None is not allowed for this quantity")
            else:
                value = self.allownone

        if isinstance(value, str):
            value = self._fromstr(value)

        if isinstance(value, Mapping) and 'str' in value:
            # as typed by a user
            stored_id = value.get('id')
            value = self._fromstr(value['str'])

        if isinstance(value, Mapping):
            value = decompress(value)
            units = value.pop('units', units)
            derived = value.pop('derived', False)
            expr = value.pop('expr', None)
            if derived:
                # calculated from other variables
                if expr is not None:
                    value = AsymmetricUncertainty(
                        value['value'], value['sigma'], value.get('sigmaup'),
                        expression=LinearExpression.from_terms(expr))
                else:
                    value = AsymmetricUncertainty.without_correlations(
                        value['value'], value['sigma'], value.get('sigmaup'))
            elif 'sigma' in value:
                value = AsymmetricUncertainty(**value)
            else:
                value = value['value']

        if not isinstance(value, pint.Quantity):
            value = pint.Quantity(value, units)
        if self.forceasym and not isinstance(value.m, AsymmetricUncertainty):
            _fromstr = get_fromstr(value)
            value = pint.Quantity(AsymmetricUncertainty(value.m, 0), value.u)
            if _fromstr:
                value._fromstr = _fromstr

        if self.convert:
            value.ito(self.units)

        value = with_id(value, stored_id)
        return value if self.expressions else as_leaf(value)

    def to_mongo(self, value):
        if _fromstr := get_fromstr(value):
            if isinstance(value.m, AsymmetricUncertainty) and value.m.isleaf:
                return {'str': _fromstr, 'id': value.m.id}
            return _fromstr
        if value is None:
            return value
        result = dict(value=value.m)
        if isinstance(value.m, AsymmetricUncertainty):
            result = self._au_to_mongo(value.m)
        if not value.dimensionless:
            result['units'] = utostr(value.u)
        return compress(result)

    def _au_to_mongo(self, au: AsymmetricUncertainty) -> dict:
        if au.isleaf or not self.expressions:
            return as_leaf(au).todict()
        result = au.todict()
        result['derived'] = True
        if self.expressions == 'inline':
            result['expr'] = au.serialize_expression()
        return result

    def validate(self, value):
        if value is None and self.allownone:
            return
        if not isinstance(value, pint.Quantity):
            self.error(f'Value must be a pint Quantity, got {value}')
        if self.units is not None and not value.is_compatible_with(self.units):
            self.error(f'Value must have units compatible with {self.units}')
        if self.forceasym and not isinstance(value.m, AsymmetricUncertainty):
            self.error('Numeric part must be an AsymmetricUncertainty')

    def prepare_query_value(self, op, value):
        return self.to_mongo(value)

    # make sure values are converted when assigned, not just in constructor
    def __set__(self, instance, value):
        return super().__set__(instance, self.to_python(value))


class UncertainQuantityField(QuantityField):
    """ A quantity field with enforced (asymmetric) uncertainty """
    def __init__(self, *args, **kwargs):
        kwargs['forceasym'] = True
        super().__init__(*args, **kwargs)


class HistogramField(UncertainQuantityField):
    def __init__(self, *args,
                 binsunit: Optional[UnitType] = None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.binsunit = binsunit

    def to_python(self, value):
        if value is None:
            return value
        if isinstance(value, (bytes, Mapping)):
            value = decompress(value)
        if isinstance(value, Mapping):
            try:
                bins = pint.Quantity(value.pop('bins'),
                                     value.pop('binsunit', self.binsunit))
            except TypeError:
                bins = None
            hist = QuantityField.to_python(self, value)
            value = Histogram(hist, bins)
        if not isinstance(value, Histogram):
            # shouldn't get here...
            self.error(f"Unhandled value for HistogramField {value}")
        if not isinstance(value.hist, pint.Quantity):
            value.hist = pint.Quantity(value.hist, self.units)
        if not isinstance(value.bin_edges, pint.Quantity):
            value.bin_edges = pint.Quantity(value.bin_edges, self.binsunit)
        if not isinstance(value.hist.m, AsymmetricUncertainty):
            value.hist = pint.Quantity(AsymmetricUncertainty(value.hist.m, 0),
                                       value.hist.u)
        if self.convert:
            value.hist.ito(self.units)
            if self.binsunit is not None:
                value.bin_edges.ito(self.binsunit)
        return value

    def to_mongo(self, value):
        result = QuantityField.to_mongo(self, value.hist)
        try:
            result['bins'] = value.bin_edges.m
            if not value.bin_edges.dimensionless:
                result['binsunit'] = utostr(value.bin_edges.u)
        except AttributeError:
            result['bins'] = value.bin_edges
        return compress(result)

    def validate(self, value):
        if value is None and self.allownone:
            return
        if not isinstance(value, Histogram):
            self.error("Value must be a Histogram object")
        super().validate(value.hist)
        if len(value.hist) != len(value.bin_edges)-1:
            self.error("Histogram and bin lengths do not match")
        if self.binsunit is not None:
            try:
                if not value.bin_edges.is_compatible_with(self.binsunit):
                    self.error(f"Bins units {value.bin_edges.u} not compatible"
                               f" with {self.binsunit}")
            except AttributeError:
                pass
