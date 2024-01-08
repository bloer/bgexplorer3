import pint
from typing import Union
import operator
from pint import Quantity
from pint.errors import DimensionalityError
from mongoengine.errors import ValidationError
from mongoengine import EmbeddedDocument, StringField, URLField

units = pint.UnitRegistry()
pint.set_application_registry(units)
units.default_format = '.2g~C'
units.load_definitions([
    "dru = 1./(kg * keV * day) = dru = DRU",
    "kky = kg * keV * year = kky = kg_keV_yr",
    "ppm = 1e-6 = ppm = parts_per_million",
    "ppb = 1e-9 = ppb = parts_per_billion",
    "ppt = 1e-12 = ppt = parts_per_trillion",
    "ppq = 1e-15 = ppq = parts_per_quadrillion",
])


def validate_unit(value, unit, allow_none=True):
    if value is None and not allow_none:
        raise ValidationError("Unit to validate must not be None")
    if not units(unit).check(value):
        raise ValidationError(f"Value {value} does not match unit {unit}")


def opnone(a, b, op):
    if a is None:
        return b
    elif b is None:
        return a
    return op(a, b)

def addnone(a, b):
    """ Add two values allowing one or both to be None """
    return opnone(a, b, operator.add)

def multnone(a, b):
    return opnone(a, b, operator.mul)


class PublicationInfo(EmbeddedDocument):
    shortlabel = StringField(verbose_name="Short label for reference")
    reference = StringField(verbose_name="Citation")
    url = URLField(verbose_name="URL for external reference")
    details = StringField(verbose_name="Reference details",
                          help_text="e.g. Table II, entry 45")
    org = StringField(verbose_name="Publishing Organization/Experiment")

    # TODO: add a clean that pre-pends https and checks
    # for DOIs or ArXiv identifiers in reference

