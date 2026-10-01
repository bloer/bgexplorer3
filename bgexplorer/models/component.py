from mongoengine import (EmbeddedDocument, StringField, DateField,
                         DateTimeField, DynamicEmbeddedDocument, BooleanField,
                         EmbeddedDocumentField, DictField, ValidationError,
                         EmbeddedDocumentListField, ListField, FloatField,
                         URLField, ObjectIdField,
                         PULL, NULLIFY, IntField)
from bson import ObjectId
from typing import Optional

from .verdoc import (VersionedDocument, VersionedReferenceField,
                     VersionedListField, VersionedEmbeddedDocumentListField,
                     ref_id)
from .fields import QuantityField, AttachmentsField
from .emissionspec import EmissionSpec, EmissionSource
from .cosmogenic import ActivatedMaterial
from .common import units, validate_unique_ids
from .isotope import compare_source_names


class PurchaseInfo(EmbeddedDocument):
    vendor = StringField(label='Vendor or producer')
    partnum = StringField(label="Vendor catalog number or drawing")
    link = URLField(label="Link to product website")
    batch = StringField(label='Batch number')
    order = StringField(label="Order number")
    purchaser = StringField(label="Purchaser name and contact info")
    owner = StringField(label="Owner name and contact info")
    purchased = DateField(label="Date purchased or fabricated")
    received = DateField(label="Date Received")

class HistoryEntry(DynamicEmbeddedDocument):
    date = DateTimeField(required=True)
    description = StringField(required=True)
    location = StringField()
    duration = QuantityField(units='h')
    worker = StringField()
    comment = StringField()


class LocationOverride(EmbeddedDocument):
    """ Use `location` to find HitEfficiencies for the sources of a component
    from `spec`, and/or named `source`, instead of the component's location.
    On an Assembly, `placement` limits it to that child Placement, ahead of
    the placement's location. Blank `spec` or `source` match any.
    """
    id = ObjectIdField(required=True, default=ObjectId)
    placement = ObjectIdField()
    spec = VersionedReferenceField(EmissionSpec, endpoint='emissionspec')
    source = StringField(help_text="Source name, e.g. Pb210; blank for any")
    location = StringField(
        required=True,
        label="Hit Efficiency Location",
        help_text="Key to match against locations in HitEfficieny database",
        autocomplete="hitefflocations",
    )

    def matches(self, spec, source_name: str, placement_id=None) -> bool:
        """ Does this apply to sources named `source_name` from `spec` (None
        for a component's own sources), for the component itself or, if
        `placement_id`, for that child placement?
        """
        myspec = ref_id(self._data.get('spec'))
        return (self.placement == placement_id and
                (myspec is None or myspec == ref_id(spec)) and
                (not self.source or
                 compare_source_names(self.source, source_name)))

    @property
    def specificity(self) -> int:
        """ Higher wins when more than one override matches """
        return (2 * (self._data.get('spec') is not None) +
                bool(self.source))


def _noslash(value):
    if isinstance(value, str) and value.find('/') != -1:
        raise ValidationError("Component labels cannot contain '/'")


class Component(VersionedDocument):
    name = StringField(required=True, validation=_noslash)
    description = StringField()
    notes = StringField(input_type='textarea')
    material = StringField()
    mass = QuantityField(units='kg', default=0 * units.kg)
    volume = QuantityField(units='m**3', default=0 * units.m**3)
    inner_surface_area = QuantityField(units='m**2', default=0 * units.cm**2)
    outer_surface_area = QuantityField(units='m**2', default=0 * units.cm**2)
    length = QuantityField(units='m', default=0 * units.m)
    width = QuantityField(units='m', default=0 * units.m)
    height = QuantityField(units='m', default=0 * units.m)
    location = StringField(
        label="Hit Efficiency Location",
        help_text="Key to match against locations in HitEfficieny database",
        autocomplete="hitefflocations",
    )

    hierarchy_level = IntField(default=1, help_text="how many levels of nested components are below us?")
    # TODO: need to add some assay quality info
    material_purchaseinfo = EmbeddedDocumentField(PurchaseInfo)
    purchaseinfo = EmbeddedDocumentField(PurchaseInfo)
    extra_metadata = DictField()
    attachments = AttachmentsField()
    history = EmbeddedDocumentListField(HistoryEntry)

    specs = VersionedListField(VersionedReferenceField(EmissionSpec,
                               reverse_delete_rule=PULL,
                               endpoint='emissionspec',
                               ))
    sources = EmbeddedDocumentListField(EmissionSource)
    activated_material = VersionedReferenceField(
        ActivatedMaterial, reverse_delete_rule=NULLIFY,
        endpoint='activatedmaterial',
        help_text="Cosmogenic activation rates for this component")
    location_overrides = VersionedEmbeddedDocumentListField(LocationOverride)

    meta = {'allow_inheritance': True}

    def __init__(self, *args, surface_area=None, **kwargs):
        if surface_area is not None:
            kwargs['outer_surface_area'] = surface_area
        super().__init__(*args, **kwargs)

    def __str__(self):
        return self.name or f"new {type(self).__name__}"

    @property
    def surface_area(self):
        return self.inner_surface_area + self.outer_surface_area

    def clean(self):
        super().clean()
        validate_unique_ids(self.sources, 'sources')
        validate_unique_ids(self.location_overrides, 'location_overrides')
        # overrides for specs or placements that were removed go with them
        specs = {None: self._data.get('specs') or []}
        for placement in getattr(self, 'children', None) or []:
            specs[placement.id] = placement.component._data.get('specs') or []
        self.location_overrides = [
            o for o in self.location_overrides
            if o.placement in specs and
            (o._data.get('spec') is None or ref_id(o._data.get('spec')) in
             {ref_id(spec) for spec in specs[o.placement]})]
        # for everything, the component or placement location does that
        for i, override in enumerate(self.location_overrides):
            if not override.source and override._data.get('spec') is None:
                raise ValidationError(
                    "Choose a spec or source to override, or set the "
                    "component or placement location instead",
                    field_name=f'location_overrides.{i}.location')

    def find_location_override(self, spec, source_name: str,
                               placement_id=None
                               ) -> Optional[LocationOverride]:
        """ The most specific of our location_overrides matching sources
        named `source_name` from `spec`, for us or for child `placement_id`
        """
        matches = [o for o in self.location_overrides
                   if o.matches(spec, source_name, placement_id)]
        return max(matches, key=lambda o: o.specificity, default=None)

    def find_parents(self):
        """ Locate all Assemblies with a Placement pointing to this component
        """
        qs = Assembly.objects()
        if self.active_version:
            qs = qs.select_version(self.active_version)
        return qs(children__component=self)

    # TODO: add additional EmissionSources just like emissionspec


def _ispositive(value):
    if value <= 0:
        raise ValidationError("Value must be strictly greater than zero")


class Placement(EmbeddedDocument):
    id = ObjectIdField(required=True, default=ObjectId)
    component = VersionedReferenceField(Component, required=True, endpoint='component')
    weight = FloatField(default=1, validation=_ispositive)
    label = StringField(required=False, validation=_noslash)
    location = StringField(
        label="Hit Efficiency Location",
        help_text="Key to match against locations in HitEfficieny database",
        autocomplete="hitefflocations",
    )

    @property
    def name(self):
        return self.label or self.component.name

def test_circular_assembly(component, assemblyPath=[]):
    """ test for circular assembly chains """
    if component in assemblyPath:
        raise ValidationError("Circular assembly path detected")
    assemblyPath = [component] + assemblyPath
    try:
        for placement in component.children:
            test_circular_assembly(placement.component, assemblyPath)
    except AttributeError:
        pass

class Assembly(Component):
    children = VersionedEmbeddedDocumentListField(Placement)
    meta = {
        'indexes': ['children.component'],
    }
    # TODO: can't use a reverse delete rule here, so need a signal
    # handler for whenever a child gets deleted

    def __init__(self, *args, components=None, **kwargs):
        if components is not None:
            if 'children' in kwargs:
                raise ValueError("'children' and 'components' both provided")
            kwargs['children'] = [Placement(component=c) for c in components]
        super().__init__(*args, **kwargs)

    def sumoverchildren(self, attr):
        return sum(getattr(p.component, attr) * p.weight
                   for p in self.children)

    def clean(self):
        # TODO: the calc for hierarchy_level below won't work if user adds
        # new placements to an already-placed child
        # could run the appropriate query on-demand, but then we couldn't
        # query against it. Maybe add to the sourceterm calc?

        super().clean()
        validate_unique_ids(self.children, 'children')
        # test for circular references
        # TODO: this is pretty expensive...
        test_circular_assembly(self)

        if self.children:
            for attr in ('mass', 'volume', 'inner_surface_area',
                         'outer_surface_area'):
                setattr(self, attr, self.sumoverchildren(attr))
            self.hierarchy_level = max(placement.component.hierarchy_level
                                       for placement in self.children) + 1
        else:
            self.hierarchy_level = 1
            self.mass = 0*units.kg
            self.volume = 0*units.m**3
            self.inner_surface_area = 0*units.cm**2
            self.outer_surface_area = 0*units.cm**2
