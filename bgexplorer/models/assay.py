from mongoengine import (EmbeddedDocument, StringField, EmbeddedDocumentField,
                         EmbeddedDocumentListField, DateField, IntField,
                         DictField)
from .emissionspec import EmissionSpec
from .fields import UncertainQuantityField, AttachmentsField
from .common import PublicationInfo


class SampleInfo(EmbeddedDocument):
    id = StringField(verbose_name="Sample ID")
    name = StringField(verbose_name="Sample name")
    description = StringField(verbose_name="Sample Description")
    vendor = StringField(verbose_name='Vendor/producer')
    partnum = StringField(verbose_name="Vendor part number/identifier")
    batch = StringField(verbose_name='Batch number/ID')
    purchased = DateField(verbose_name="Purchase date")
    received = DateField(verbose_name="Date received")
    ownerorg = StringField(verbose_name="Owning institution")
    owner = StringField(verbose_name='Sample owner')
    ownercontact = StringField(verbose_name='Owner contact info')
    notes = StringField(verbose_name='Additional Notes')


class MeasurementRequest(EmbeddedDocument):
    requestor = StringField(help_text="Name of person who requested assay")
    requestorcontact = StringField(verbose_name="Requestor contact info")
    date_requested = DateField(verbose_name="Date request was created")
    targetsensitivity = StringField(verbose_name="Targeted sensitivity")
    notes = StringField(verbose_name="Additional Notes")


class MeasurementResult(EmbeddedDocument):
    replicate = IntField(default=1)
    isotope = StringField(required=True)
    value = UncertainQuantityField(required=True)


class MeasurementInfo(EmbeddedDocument):
    id = StringField(verbose_name="Measurement ID")
    technique = StringField(verbose_name='Measurement technique')
    institution = StringField(verbose_name='Institution/Location')
    instrument = StringField(verbose_name='Instrument used')
    date_received = DateField(verbose_name="Date sample received")
    date_measured = DateField(verbose_name='Measurement date')
    operator = StringField(help_text='Name of person who made measurement')
    operatorcontact = StringField(verbose_name='Operator contact info')
    notes = StringField(verbose_name='Additional Notes')
    results = EmbeddedDocumentListField(MeasurementResult)


class Assay(EmissionSpec):
    """ An emission spec based on material or surface assay measurement """
    sample = EmbeddedDocumentField(SampleInfo,
                                   verbose_name="Sample Information")
    request = EmbeddedDocumentField(MeasurementRequest)
    measurement = EmbeddedDocumentField(MeasurementResult,
                                        verbose_name="Measurement details")
    publication = EmbeddedDocumentField(PublicationInfo)
    radiopurityid = StringField(verbose_name="radiopurity.org database id")
    extra_metadata = DictField()
    attachments = AttachmentsField()
