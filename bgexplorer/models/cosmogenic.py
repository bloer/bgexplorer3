from bson import ObjectId
from mongoengine import (EmbeddedDocument, StringField, EmbeddedDocumentField,
                         EmbeddedDocumentListField, ObjectIdField,
                         ValidationError)

from .common import PublicationInfo, validate_unique_ids
from .verdoc import VersionedDocument
from .fields import UncertainQuantityField, AttachmentsField
from .isotope import get_halflife, get_tau, compare_source_names


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
