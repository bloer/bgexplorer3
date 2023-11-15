from mongoengine import Document, StringField, EnumField
from enum import Enum

from fields import QuantityField
from common import units, validate_unit

class Distribution(Enum):
    BULK = 'bulk'
    SURFACE = 'surface'
    SURFACE_IN = 'surface_in'
    SURFACE_OUT = 'surface_out'
    FLUX = 'flux'
    OTHER = 'other'


class BgSource(Document):
    name = StringField(max_length=32, required=True)
    distribution = EnumField(Distribution, default=Distribution.BULK)
    particle = StringField(max_length=2)
    spectrum = StringField(max_length=2)
    rate = QuantityField(required=True)
    assay = ReferenceField('Assay', reverse_delete_rule=CASCADE)
    generated_from = ReferenceField('BgSource', reverse_delete_rule=CASCADE)
    weight = FloatField(default=1)

    def clean(self):
        if self.distribution == Distribution.BULK:
            validate_unit(self.rate, 'Bq/kg')
        elif self.distribution in [Distribution.SURFACE,
                                   Distribution.SURFACE_IN,
                                   Distribution.SURFACE_OUT]:
            validate_unit(self.rate, 'Bq/cm**2')
        elif self.distribution == Distribution.FLUX:
            try:
                validate_unit(self.rate, '1/cm**s/s')
            except ValidationError:
                validate_unit(self.rate, '1/cm**s/s/sr')

    def generate_extra(self, assay):
        """ Generate extra BgSources based on an original contaminant.
        For example, for U238, typically generate spontaneous fission
        and/or alpha,n neutron spectra or U235.
        """
        pass

    def emissionrate(self, component):
        multiplier = 1
        if self.distribution == Distribution.BULK:
            multiplier = component.mass
        elif self.distribution == Distribution.SURFACE:
            multiplier = component.surface_area
        elif self.distribution == Distribution.SURFACE_IN:
            multiplier = component.surface_in
        elif self.distribution == Distribution.SURFACE_OUT:
            multiplier = component.surface_out
        return self.rate * weight * multiplier


class Assay(Document):
    name = StringField(max_length=32)
    sources = ListField(ReferenceField(BgSource))

    def clean(self):
        for source in self.sources:
            source.save()
