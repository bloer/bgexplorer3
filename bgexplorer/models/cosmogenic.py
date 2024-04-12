from math import log, exp
from mongoengine import (EmbeddedDocument, StringField, EmbeddedDocumentField,
                         EmbeddedDocumentListField, ValidationError)

from .common import units, PublicationInfo
from .verdoc import VersionedDocument, VersionedReferenceField
from .fields import QuantityField, UncertainQuantityField
from .emissions import EmissionSpec
from .isotope import get_halflife


_ln2 = log(2)


def _has_halflife(source: str) -> None:
    if get_halflife(source) is None:
        raise ValidationError(f"{source} is not a radioactive isotope")


class CosmogenicIsotope(EmbeddedDocument):
    id = ObjectIdField(default=ObjectId)
    isotope = StringField(max_length=6, required=True,
                          validation=_has_halflife)
    activationrate = UncertainQuantityField(required=True,
                                            units="1/g/day",
                                            allownone=False)

    def rate(self, exposure, cooldown=0*units.s, integration=0*units.s):
        """ Average differential decay rate given a simple exposure history
        returns in units of mBq/kg
        """
        tau = get_halflife(self.isotope) / _ln2
        if integration <= 0*units.day:
            integration = 1*units.ms
        R0 = self.activationrate * (1 - exp(-exposure / tau))
        a = cooldown
        b = a + integration
        return R0 * (exp(-a / tau) - exp(-b / tau)) * tau / integration


class ActivatedMaterial(VersionedDocument):
    material = StringField(required=True)
    isotopes = EmbeddedDocumentListField(CosmogenicIsotope)
    comment = StringField()
    publication = EmbeddedDocumentField(PublicationInfo)


class ActivationTimeSpec(EmissionSpec):
    activation = QuantityField(required=True, units='day')
    cooldown = QuantityField(units='day', default=0*units.day,
                             allownone=False)
    integration = QuantityField(units='day', default=0*units.day,
                                allownone=False)
    comment = StringField()

    def get_sources(component):
        material = component.material_activation
        if not material:
            return []
        return [EmissionSpec(id=source.id, name=source.isotope,
                             rate=source.rate(self.activation, self.cooldown,
                                              self.integration),
                             category='activaton',
                             multiplier=Multiplier.mass)
                 for source in material.isotopes]

