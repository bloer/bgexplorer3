from mongoengine import (EmbeddedDocument, StringField, DateField,
                         DateTimeField, DynamicEmbeddedDocument, BooleanField,
                         EmbeddedDocumentField, DictField, ValidationError,
                         EmbeddedDocumentListField, ListField, FloatField,
                         CASCADE, PULL, signals)

from .verdoc import VersionedDocument, VersionedReferenceField
from .fields import QuantityField, AttachmentsField
from .emissionspec import EmissionSpec, EmissionSource
from .common import units


class PurchaseInfo(EmbeddedDocument):
    vendor = StringField(verbose_name='Vendor/producer')
    partnum = StringField(verbose_name="Vendor part number or drawing")
    material_batch = StringField(verbose_name="material batch number")
    batch = StringField(verbose_name='Fabrication batch number')
    purchased = DateField(verbose_name="Purchase date")
    received = DateField(verbose_name="Date received")
    fabricated = DateField(verbose_name="Fabrication date")
    owner = StringField(verbose_name='Sample owner')
    ownercontact = StringField(verbose_name='Owner contact info')


class HistoryEntry(DynamicEmbeddedDocument):
    description = StringField(required=True)
    date = DateTimeField(required=True)
    location = StringField()
    duration = QuantityField(units='s')
    comment = StringField()
    person = StringField()
    enteredby = StringField()


def _noslash(value):
    if isinstance(value, str) and value.find('/') != -1:
        raise ValidationError("Component labels cannot contain '/'")


class Component(VersionedDocument):
    name = StringField(required=True, validation=_noslash)
    description = StringField()
    notes = StringField()

    mass = QuantityField(units='kg', default=0 * units.kg)
    volume = QuantityField(units='m**3', default=0 * units.m**3)
    inner_surface_area = QuantityField(units='m**2', default=0 * units.cm**2)
    outer_surface_area = QuantityField(units='m**2', default=0 * units.cm**2)
    treat_surface_as_bulk = BooleanField(default=False)
    distribution = StringField(
        help_text="Override default distribution from rate units"
    )

    location = StringField(
        verbose_name="HitEfficiency Location",
        help_text="Key to match against locations in HitEfficieny database"
    )

    purchaseinfo = EmbeddedDocumentField(PurchaseInfo)
    extra_metadata = DictField()
    attachments = AttachmentsField()
    history = EmbeddedDocumentListField(HistoryEntry)

    reference_specs = ListField(VersionedReferenceField(EmissionSpec,
                                reverse_delete_rule=PULL))
    sources = EmbeddedDocumentListField(EmissionSource)

    meta = {'allow_inheritance': True}

    def __init__(self, *args, surface_area=None, **kwargs):
        if surface_area is not None:
            kwargs['outer_surface_area'] = surface_area
        super().__init__(*args, **kwargs)

    @property
    def surface_area(self):
        return self.inner_surface_area + self.outer_surface_area

    def find_parents(self):
        """ Locate all Assemblies with a Placement pointing to this component
        """
        qs = Placement.objects()
        if self.active_version:
            qs = qs.select_version(self.active_version)
        return qs(component=self).distinct('parent')

    # TODO: add additional EmissionSources just like emissionspec


def _ispositive(value):
    if value <= 0:
        raise ValidationError("Value must be strictly greater than zero")


class Assembly(Component):
    children = ListField(VersionedReferenceField('Placement'))
    # TODO: can't use a reverse delete rule here, so need a signal
    # handler for whenever a child gets deleted

    def __init__(self, *args, components=None, **kwargs):
        if components is not None:
            if 'children' in kwargs:
                raise ValueError("'children' and 'components' both provided")
            kwargs['children'] = [Placement(parent=self, component=c)
                                  for c in components]
        super().__init__(*args, **kwargs)

    def sumoverchildren(self, attr):
        return sum(getattr(p.component, attr) * p.weight
                   for p in self.children)

    def clean(self):
        super().clean()
        for placement in self.children:
            placement.parent = self
            placement.save()
        if self.children:
            for attr in ('mass', 'volume', 'inner_surface_area',
                         'outer_surface_area'):
                setattr(self, attr, self.sumoverchildren(attr))
        else:
            self.mass = 0*units.kg
            self.volume = 0*units.m**3
            self.inner_surface_area = 0*units.cm**2
            self.outer_surface_area = 0*units.cm**2


def post_save_assembly(sender, document, **kwargs):
    assembly = document
    Placement.select_version(assembly.active_version)(
        parent=assembly,
        original_id__nin=[p.original_id for p in assembly.children]
    ).delete()


signals.post_save.connect(post_save_assembly, sender=Assembly)


class Placement(VersionedDocument):
    parent = VersionedReferenceField('Assembly', required=True,
                                     reverse_delete_rule=CASCADE)
    component = VersionedReferenceField(Component, required=True,
                                        reverse_delete_rule=CASCADE)
    weight = FloatField(default=1, validation=_ispositive)
    label = StringField(required=False, validation=_noslash)
    location = StringField()

    @property
    def name(self):
        return self.label or self.component.name


Placement.register_delete_rule(Assembly, 'children', PULL)
