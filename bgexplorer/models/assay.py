from mongoengine import (EmbeddedDocument, StringField, EmbeddedDocumentField,
                         EmbeddedDocumentListField)
from .emissionspec import EmissionSpec
from .fields import UncertainQuantityField, AttachmentsField


class SampleInfo(EmbeddedDocument):
    sampleid = StringField(verbose_name="Sample ID")
    description = StringField(verbose_name='Description')
    vendor = StringField(verbose_name='Vendor/producer')
    partnum = StringField(verbose_name="Vendor part number/identifier")
    batch = StringField(verbose_name='Batch number/ID')
    purchased = DateField(verbose_name="Purchase date")
    received = DateField(verbose_name="Date received")
    owner = StringField(verbose_name='Sample owner')
    ownercontact = StringField(verbose_name='Owner contact info')
    notes = StringField(verbose_name='Additional Notes')


class MeasurementResult(EmbeddedDocument):
    label = StringField(required=True)
    value = UncertainQuantityField(required=True)


class MeasurementInfo(EmbeddedDocument):
    technique = StringField(verbose_name='Measurement technique')
    institution = StringField(verbose_name='Institution/Location')
    instrument = StringField(verbose_name='Instrument used')
    received = SringField(verbose_name="Date sample received")
    date = StringField(verbose_name='Measurement date')
    operator = StringField(verbose_name='Operator',
                           help_text='Name of person who made measurement')
    operatorcontact = StringField(verbose_name='Operator contact info')
    notes = StringField(verbose_name='Additional Notes')
    results = EmbeddedDocumentListField(MeasurementResult)


class Assay(EmissionSpec):
    """ An emission spec based on material or surface assay measurement """
    sample = EmbeddedDocumentField(SampleInfo,
                                   verbose_name="Sample Information")
    measurement = EmbeddedDocumentField(MeasurementResult,
                                        verbose_name="Measurement details")
    extra_metadata = DictField()
    attachments = AttachmentsField()
