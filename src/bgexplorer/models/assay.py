from mongoengine import (EmbeddedDocument, StringField, EmbeddedDocumentField,
                         EmbeddedDocumentListField, DateField, IntField,
                         DictField, URLField, MapField)
from .emissionspec import EmissionSpec, SourceCategory
from .fields import UncertainQuantityField, QuantityField
from .common import PublicationInfo


class SampleInfo(EmbeddedDocument):
    id = StringField(label="Sample ID")
    name = StringField(label="Sample name")
    description = StringField(label="Sample Description")
    material = StringField()
    mass = QuantityField(units='kg')
    vendor = StringField(label='Vendor/producer')
    partnum = StringField(label="Vendor part number/identifier")
    link = URLField(label="Link to product website")
    batch = StringField(label='Batch number/ID')
    purchased = DateField(label="Purchase date")
    received = DateField(label="Date received")
    ownerorg = StringField(label="Owning institution")
    owner = StringField(label='Sample owner')
    ownercontact = StringField(label='Owner contact info')
    notes = StringField(label='Additional Notes')


class MeasurementRequest(EmbeddedDocument):
    requestor = StringField(help_text="Name of person who requested assay")
    requestorcontact = StringField(label="Requestor contact info")
    date_requested = DateField(label="Date request was created")
    targetsensitivity = StringField(label="Targeted sensitivity")
    notes = StringField(label="Additional Notes")


class MeasurementResult(EmbeddedDocument):
    replicate = IntField(default=1)
    mass = QuantityField(units='kg')
    isotopes = MapField(UncertainQuantityField())

    def __str__(self):
        return str({k:str(v) for k,v in self.isotopes.items()})

class MeasurementInfo(EmbeddedDocument):
    id = StringField(label="Measurement ID")
    technique = StringField(label='Measurement technique')
    institution = StringField(label='Institution/Location')
    instrument = StringField(label='Instrument used')
    date_received = DateField(label="Date sample received")
    date_measured = DateField(label='Measurement date')
    count_time = QuantityField(units='hour')
    operator = StringField(help_text='Name of person who made measurement')
    operatorcontact = StringField(label='Operator contact info')
    notes = StringField(label='Additional Notes')
    results = EmbeddedDocumentListField(MeasurementResult)


class Assay(EmissionSpec):
    """ An emission spec based on material or surface assay measurement """
    sample = EmbeddedDocumentField(SampleInfo,
                                   label="Sample Information")
    request = EmbeddedDocumentField(MeasurementRequest)
    measurement = EmbeddedDocumentField(MeasurementInfo,
                                        label="Measurement details")
    publication = EmbeddedDocumentField(PublicationInfo)
    radiopurityid = StringField(label="radiopurity.org database id")
    extra_metadata = DictField()

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('category', SourceCategory.assay)
        super().__init__(*args, **kwargs)
