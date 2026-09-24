import mongoengine as me
from pint.errors import PintError
from ..models.verdoc import VersionedDocument
from ..models.fields import get_fromstr

def input_type(field):
    """ Determine the <input> type for the given field """
    if hasattr(field, 'input_type'):
        return field.input_type
    if (isinstance(field, (me.ObjectIdField, ))
            or getattr(field, 'hidden', False)):
        return 'hidden'
    if isinstance(field, me.ReferenceField):
        return 'reference'
    if isinstance(field, me.EnumField) or field.choices:
        return 'select'
    if isinstance(field, me.EmailField):
        return 'email'
    if isinstance(field, me.URLField):
        return 'url'
    if isinstance(field, (me.IntField, me.FloatField)):
        return 'number'
    if isinstance(field, me.DateField):
        return 'date'
    if isinstance(field, me.DateTimeField):
        return 'datetime-local'
    if isinstance(field, me.BooleanField):
        return 'checkbox'
    return 'text'

def input_value(value):
    """ Determine the <input> value for a field value """
    if isinstance(value, me.Document):
        return getattr(value, 'original_id', None) or value.id
    if isinstance(value, me.EmbeddedDocument):
        return getattr(value, 'id', None) or value
    return get_fromstr(value) or value


LISTFIELDS_KEY = '_listfields'


FALSE_STRINGS = ('', 'false', 'off', 'no', '0')
# errors from a field's to_python for unparseable input
CONVERSION_ERRORS = (me.ValidationError, ValueError, TypeError,
                     AttributeError, PintError)


def parse_bool(value: str) -> bool:
    """ Parse a form value for a BooleanField """
    return value.strip().lower() not in FALSE_STRINGS


def _convert(field, raw, errors, errorkey):
    """ Convert a raw form string for `field`. If `errors` is a dict, store
    conversion errors in it under `errorkey` and return `_INVALID`
    """
    if raw == '':
        return raw
    if isinstance(field, me.BooleanField):
        return parse_bool(raw)
    try:
        return field.to_python(raw)
    except CONVERSION_ERRORS as e:
        if errors is None:
            raise
        errors[errorkey] = getattr(e, 'message', None) or str(e)
        return _INVALID


_INVALID = object()


def _map_keys(form, fullfieldname):
    """ Keys of a MapField in the form, named like `field[key]` or
    `field[key].subfield`, in form order
    """
    start = fullfieldname + '['
    keys = {}
    for name in form.keys():
        if name.startswith(start) and ']' in name[len(start):]:
            keys[name[len(start):].rsplit(']', 1)[0]] = None
    return list(keys)


def update_object(obj, form, prefix=None, index=0, errors=None, path=None):
    """ Update the fields of the document object from the values in the form

    Fields not present in the form are left unchanged. Since an empty list
    submits no values, list and map fields are only updated if they have
    values or are named in the form's `LISTFIELDS_KEY` entries. List and map
    fields are replaced entirely by the values in the form.

    Entries of a MapField are named `field[key]`, or `field[key].subfield`
    for maps of EmbeddedDocuments.

    If `errors` is a dict, values that can't be converted are left
    unchanged, and the messages stored in `errors` keyed by the dotted path
    of the field, with list indices and map keys as path elements (like
    the keys of a flattened ValidationError). Otherwise they raise.
    """
    listfields = form.getlist(LISTFIELDS_KEY)
    if path is None:
        path = prefix.split('.') if prefix else []
    for fieldname in obj._fields_ordered:
        field = obj._fields[fieldname]
        fullfieldname = '.'.join([prefix, fieldname]) if prefix else fieldname
        errorkey = '.'.join(path + [fieldname])
        value = None
        if isinstance(field, me.EmbeddedDocumentField):
            currentval = getattr(obj, fieldname, None)
            if not isinstance(currentval, me.EmbeddedDocument):
                currentval = field.document_type()
            value = update_object(currentval, form, prefix=fullfieldname,
                                  index=index, errors=errors,
                                  path=path + [fieldname])
            # TODO: handle null EmbeddedDocuments
        elif isinstance(field, me.EmbeddedDocumentListField):
            # TODO: can't nest lists or this will break
            # the EmbeddedDocument must have a default-constructor
            # use the first subfield to determine the list length
            doctype = field.field.document_type
            firstfield = '.'.join([fullfieldname, doctype._fields_ordered[0]])
            if firstfield not in form and fullfieldname not in listfields:
                continue
            value = [update_object(doctype(), form, prefix=fullfieldname,
                                   index=index, errors=errors,
                                   path=path + [fieldname, str(index)])
                     for index in range(len(form.getlist(firstfield)))]
        elif isinstance(field, me.MapField):
            keys = _map_keys(form, fullfieldname)
            if not keys and fullfieldname not in listfields:
                continue
            subfield = field.field
            value = {}
            for key in keys:
                name = f"{fullfieldname}[{key}]"
                if isinstance(subfield, me.EmbeddedDocumentField):
                    value[key] = update_object(
                        subfield.document_type(), form, prefix=name,
                        index=index, errors=errors,
                        path=path + [fieldname, key])
                else:
                    converted = _convert(subfield, form.get(name, ''), errors,
                                         f"{errorkey}.{key}")
                    if converted is not _INVALID:
                        value[key] = None if converted == '' else converted
        elif isinstance(field, me.ListField):
            if fullfieldname not in form and fullfieldname not in listfields:
                continue
            value = []
            for i, raw in enumerate(form.getlist(fullfieldname)):
                # skip blank inputs
                if raw.strip() == '':
                    continue
                converted = _convert(field.field, raw, errors,
                                     f"{errorkey}.{i}")
                if converted is not _INVALID:
                    value.append(converted)
        elif fullfieldname not in form:
            continue
        else:
            raw = form.getlist(fullfieldname)
            if index >= len(raw):
                value = None
            else:
                value = _convert(field, raw[index], errors, errorkey)
                if value is _INVALID:
                    continue
        # sometimes an empty string is a default that shouldn't be set, like id
        # sometimes the user will actually be nullifying something
        # how do we tell the difference?  Make a special default value?
        if value == '':
            delattr(obj, fieldname)
        else:
            setattr(obj, fieldname, value)
    return obj
