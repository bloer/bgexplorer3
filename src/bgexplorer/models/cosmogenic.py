from bson import ObjectId
from mongoengine import (EmbeddedDocument, StringField, EmbeddedDocumentField,
                         EmbeddedDocumentListField, ObjectIdField, FloatField,
                         ValidationError)

from .common import PublicationInfo, validate_unique_ids, units
from .verdoc import VersionedDocument, VersionedReferenceField
from .fields import UncertainQuantityField, AttachmentsField, QuantityField
from .emissionspec import (EmissionSpec, EmissionSource, Multiplier,
                           SourceCategory)
from .isotope import (get_halflife, get_tau, compare_source_names,
                      decayed_fraction)


def _has_halflife(source: str) -> None:
    if get_halflife(source) is None:
        raise ValidationError(f"{source} is not a radioactive isotope")


class CosmogenicIsotope(EmbeddedDocument):
    id = ObjectIdField(required=True, default=ObjectId)
    isotope = StringField(required=True, validation=_has_halflife)
    activationrate = UncertainQuantityField(
        required=True, units='1/kg/day',
        label="Activation rate",
        help_text="Production rate at sea level, e.g. atoms/kg/day")
    comment = StringField()

    @property
    def tau(self):
        """ Mean lifetime of the isotope """
        return get_tau(self.isotope)

    def activity(self, periods) -> units.Quantity:
        """ Specific activity at the end of the ActivationPeriods `periods`,
        starting from none
        """
        activity = 0 * self.activationrate
        for period in periods:
            decayed = decayed_fraction(period.duration, self.tau)
            # what was there decays, and builds up towards the rate
            activity = (activity * (1 - decayed) +
                        self.activationrate * period.factor * decayed)
        return activity.to('Bq/kg')


class ActivatedMaterial(VersionedDocument):
    """ The cosmogenic isotopes produced in a material and their sea level
    activation rates
    """
    name = StringField(required=True)
    material = StringField()
    description = StringField()
    comment = StringField()
    isotopes = EmbeddedDocumentListField(CosmogenicIsotope)
    publication = EmbeddedDocumentField(PublicationInfo)
    attachments = AttachmentsField()

    def __str__(self):
        return self.name or f"new {type(self).__name__}"

    def clean(self):
        super().clean()
        validate_unique_ids(self.isotopes, 'isotopes')
        for i, iso in enumerate(self.isotopes):
            if any(compare_source_names(iso.isotope, other.isotope)
                   for other in self.isotopes[:i]):
                raise ValidationError(f"Isotope {iso.isotope} is listed "
                                      "more than once",
                                      field_name='isotopes')


class ActivationPeriod(EmbeddedDocument):
    """ A time spent at a constant activation rate """
    id = ObjectIdField(required=True, default=ObjectId)
    description = StringField(help_text="e.g. surface storage, flight")
    location = StringField()
    duration = QuantityField(required=True, units='day')
    factor = FloatField(default=1, help_text="Activation rate relative to sea "
                        "level: 1 at the surface, 0 underground")


class CosmogenicActivation(EmissionSpec):
    """ The isotopes activated in a material by its history of exposure to
    cosmic rays. The periods are in order, and gaps between them are ignored.
    Cooling down underground is a period with factor 0.
    """
    COMPUTED_SOURCES = True
    material = VersionedReferenceField(ActivatedMaterial, required=True,
                                       endpoint='activatedmaterial')
    periods = EmbeddedDocumentListField(ActivationPeriod)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('category', SourceCategory.activation)
        kwargs.setdefault('multiplier', Multiplier.mass)
        super().__init__(*args, **kwargs)

    def clean(self):
        sources = []
        # missing values are reported by field validation
        if (isinstance(self.material, ActivatedMaterial) and
                all(p.duration is not None and p.factor is not None
                    for p in self.periods)):
            total = sum((p.duration for p in self.periods), 0 * units.day)
            comment = (f"{self.material.name} activated over "
                       f"{len(self.periods)} period(s), {total:~.3g} total")
            sources = [EmissionSource(name=iso.isotope,
                                      category=SourceCategory.activation,
                                      multiplier=self.multiplier,
                                      rate=iso.activity(self.periods),
                                      comment=comment)
                       for iso in self.material.isotopes]
        self._set_computed_sources(sources)
        super().clean()
