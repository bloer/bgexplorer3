from functools import cached_property, reduce
import operator
import mongoengine
from mongoengine import (ListField, EmbeddedDocumentField, FloatField,
                         StringField, BooleanField, MapField, Document,
                         SortedListField, CASCADE, PULL, ReferenceField)
from .verdoc import VersionedDocument, VersionedReferenceField
from .component import Component, Placement, Assembly
from .emissionspec import EmissionSpec, EmissionSource, Multiplier
from .fields import QuantityField, UncertainQuantityField, HistogramField
from .hiteff import HitEfficiency
from .isotope import concentration_to_rate
from .common import units, addnone, multnone

from typing import List, Optional


class SourceTerm(VersionedDocument):
    # these fields are keys used for finding SourceTerms in the db
    assemblyRoot = VersionedReferenceField(Component, required=True,
                                           reverse_delete_rule=CASCADE)
    assemblyPath = ListField(VersionedReferenceField(Placement),
                             reverse_delete_rule=CASCADE)
    source = EmbeddedDocumentField(EmissionSource, required=True)
    spec = VersionedReferenceField(EmissionSpec, reverse_delete_rule=CASCADE)
    # These are used to find hiteffs
    location = StringField()
    distribution = StringField()
    # used to calculate results
    weight = FloatField(default=1)
    rate_multiplier = QuantityField(default=1)
    hiteffs = ListField(VersionedReferenceField(HitEfficiency,
                                                reverse_delete_rule=PULL))
    hiteffs_auto = BooleanField(default=True)
    livetimes = ListField(QuantityField(units='day', allownone=True))
    # partially denormalized info
    # emissionrate = UncertainQuantityField(required=True)
    componentName = StringField()
    assemblyPathStr = StringField()

    meta = {'indexes': ['assemblyPath', 'source.id', 'hiteffs']}

    @property
    def component(self):
        try:
            return self.assemblyPath[-1].component
        except IndexError:
            return self.assemblyRoot

    @cached_property
    def emissionrate(self):
        if self.source.rate is None:
            return None
        rate = self.source.rate
        # convert ppb-like units to specific activity
        if (self.source.multiplier is Multiplier.mass and
                rate.u in ('ppt', 'ppb', 'ppq', 'percent')):
            rate = concentration_to_rate(rate, self.source.name)
        return rate * self.weight * self.rate_multiplier

    def clean(self):
        super().clean()
        self.clear_results()
        # TODO: should these be cached properties rather than set by clean?
        # location is set by the component or placement closest to the leaf
        for placement in reversed(self.assemblyPath):
            self.location = (placement.component.location or
                             placement.location or
                             placement.parent.location)
            if self.location:
                break
        self.location = self.location or self.assemblyRoot.location
        # override distribution based on component settings
        self.distribution = self.source.multiplier.default_distribution()
        if (self.component.treat_surface_as_bulk
                and self.distribution.find('surface') != -1):
            self.distribution = 'bulk'

        self.weight = reduce(operator.mul,
                             (p.weight for p in self.assemblyPath), 1)
        self.rate_multiplier = self.source.multiplier.getvalue(self.component)
        # default to componentName for location queries if location isn't set
        # TODO: should use root rather than component?
        self.componentName = self.component.name
        self.assemblyPathStr = '/'.join([self.assemblyRoot.name] +
                                        [p.name for p in self.assemblyPath])
        # clear cached properties1
        try:
            del self.emissionrate
        except AttributeError:
            pass
        self.livetimes = [hit.get_livetime(self.emissionrate)
                          for hit in self.hiteffs]

    def find_hiteffs(self, replace: bool = True) -> List[HitEfficiency]:
        hits = HitEfficiency.select_version(self.active_version)(
            source=self.source.name,
            location=self.location or self.componentName,
            distribution=self.distribution,
            ).exclude('spectra')
        hiteffs = list(hits)
        if hiteffs and replace:
            self.hiteffs = hiteffs
            self.hiteffs_auto = False
            self.save()
            #livetimes = [hit.get_livetime(self.emissionrate)
            #             for hit in hiteffs]
            #print("\n!!!!!!!", hiteffs, livetimes)
            #self.modify(set__hiteffs=hiteffs,
            #            set__hiteffs_auto=True,
            #            set__livetimes=livetimes,
            #            )
        return hiteffs

    def clear_results(self):
        """ Remove all CalculatedResults in the database referencing
        this object
        """
        CalculatedResults.objects(sources=self).delete()

    def upsert(self, update_hiteffs: bool = True) -> 'SourceTerm':
        """ `save` only works after we have been inserted into the database
        This method will find and upate an existing match or create a new
        entry

        Returns the upserted entry without modifying self
        """
        self.validate()
        query = SourceTerm.objects(version_tags=self.active_version,
                                   assemblyRoot=self.assemblyRoot,
                                   source__id=self.source.id,
                                   spec=self.spec)
        if self.assemblyPath:
            query = query(assemblyPath__0=self.assemblyPath[0])
        update_dict = dict(set_on_insert__id=self.id,
                           set_on_insert__original_id=self.original_id,
                           set_on_insert__version_tags=[self.active_version],
                           set_on_insert__assemblyRoot=self.assemblyRoot,
                           set_on_insert__assemblyPath=self.assemblyPath,
                           set_on_insert__spec=self.spec,
                           set__source=self.source,
                           set__location=self.location,
                           set__distribution=self.distribution,
                           set__weight=self.weight,
                           set__rate_multiplier=self.rate_multiplier,
                           set__componentName=self.component.name,
                           set__assemblyPathStr=self.assemblyPathStr,
                           )
        if not self.hiteffs_auto:
            update_dict['set__hiteffs_auto'] = self.hiteffs_auto
            update_dict['set__hiteffs'] = self.hiteffs
        else:
            update_dict['set_on_insert__hiteffs_auto'] = True
        st = query.upsert_one(**update_dict)
        st.clear_results()
        if update_hiteffs and st.hiteffs_auto:
            st.find_hiteffs()
        return st

    @classmethod
    def from_component_source(cls, component, source, spec=None):
        """ Create a SourceTerm from a source associated to a component """
        st = SourceTerm(version_tag=component.active_version,
                        assemblyRoot=component,
                        source=source,
                        spec=spec,
                        ).upsert()
        return st

    @classmethod
    def from_placement(cls, chsource, parent, placement):
        """ Create a SourceTerm by appending parent info and placement details
        to one created by the child
        """
        st = SourceTerm(version_tag=chsource.active_version,
                        assemblyRoot=parent,
                        assemblyPath=[placement] + chsource.assemblyPath,
                        source=chsource.source,
                        spec=chsource.spec,
                        hiteffs_auto=chsource.hiteffs_auto,
                        hiteffs=chsource.hiteffs,
                        ).upsert()
        return st


class CalculatedResults(Document):
    """ Cache normalized HitEff results """
    # TODO: should this be a versioned document? it's acting as a cache
    # of sorts
    values = MapField(UncertainQuantityField(allownone=True),
                      required=False, default=dict)
    spectra = MapField(HistogramField(allownone=True),
                       required=False, default=dict)
    sources = SortedListField(ReferenceField(SourceTerm,
                                             reverse_delete_rule=CASCADE))
    meta = {'indexes': ['sources']}

    @classmethod
    def for_component(cls, component: Component,
                      relativeto: Optional[Component] = None):
        query = SourceTerm.select_version(component.active_version)
        if relativeto is None:
            query = query(assemblyRoot=component)
        else:
            placements = Placements.objects(
                version_tags=component.active_version,
                component=component)
            query = query(assemblyRoot=relativeto,
                          assemblyPath__in=placements)
        return cls.from_sourceterms(query)


    def ito_reduced_units(self):
        for v in self.values.values():
            try:
                v.ito_reduced_units()
            except AttributeError:
                pass
        for v in self.spectra.values():
            try:
                v.hist.ito_reduced_units()
            except AttributeError:
                pass
        return self

    def __add__(self, other):
        if not isinstance(other, CalculatedResults):
            raise TypeError('CalculatedResults can only add with others')
        # TODO: some checks to makes sure we're not duplicating sources
        # TODO: check active_version
        result = CalculatedResults(sources=self.sources + other.sources)
        for key in set(self.values).union(other.values):
            result.values[key] = addnone(self.values.get(key),
                                         other.values.get(key))
        for key in set(self.spectra).union(other.spectra):
            result.spectra[key] = addnone(self.spectra.get(key),
                                          other.spectra.get(key))
        return result.ito_reduced_units()

    @classmethod
    def _from_hiteff(cls, sourceterm: SourceTerm, hiteff: HitEfficiency
                     ) -> 'CalculatedResults':
        erate = sourceterm.emissionrate
        if not hiteff.norm.check(erate):
            raise units.DimensionalityError("incompatible emissionrate units")
        return cls(values={key: multnone(val,  erate) for key, val in
                           hiteff.values.items() if val is not None},
                   spectra={key: multnone(val, erate) for key, val in
                            hiteff.spectra.items() if val is not None},
                   ).ito_reduced_units()

    @classmethod
    def from_db(cls, sourceterms: List[SourceTerm]):
        ids = list(sorted(st.id for st in sourceterms))
        # can we do this with a match query?
        return cls.objects(__raw__={'sources': {'$eq': ids}}).first()

    @classmethod
    def from_sourceterm(cls, sourceterm: SourceTerm, allowcache: bool = True
                        ) -> 'CalculatedResults':
        """ Calculalte all results for a single SourceTerm """
        if allowcache and (result := cls.from_db([sourceterm]) is not None):
            return result
        result = sum((cls._from_hiteff(sourceterm, h)
                      for h in sourceterm.hiteffs),
                     cls(sources=[sourceterm]))
        try:
            result.ito_reduced_units()
        except AttributeError:
            pass
        return result

    @classmethod
    def from_sourceterms(cls, sourceterms: List[SourceTerm],
                         allowcache: bool = True) -> 'CalculatedResults':
        if allowcache and (result := cls.from_db(sourceterms) is not None):
            return result
        # even if subs are cached, re-calculate to pick up correlations
        result = sum((cls.from_sourceterm(st, allowcache=False)
                      for st in sourceterms),
                     cls())
        try:
            result.ito_reduced_units()
        except AttributeError:
            pass
        return result


def update_component(sender, document, **kwargs):
    component = document
    # first, generate the list of all source terms for this component
    sourceterms = []
    for spec in list(component.reference_specs) + [component]:
        for source in spec.sources:
            spec = spec if spec is not component else None
            sourceterms.append(SourceTerm.from_component_source(component,
                                                                source, spec)
                               )
    # remove any that didn't match
    SourceTerm.objects(
        version_tags=component.active_version,
        assemblyRoot=component,
        id__nin=[st.id for st in sourceterms],
        ).delete()
    # TODO: need to remove all calculated results that reference

    # do we calculate the result now, or do it on demand?
    # update all assemblies containing us
    for parent in component.find_parents():
        update_assembly(sender=None, document=parent, component=component)
    return sourceterms


def update_assembly(sender, document, component=None, placement=None,
                    **kwargs):
    assembly = document
    sourceterms = []
    for child in assembly.children:
        if (component and child.component.id != component.id or
            placement and child.id != placement.id):
            continue
        childterms = SourceTerm.select_version(assembly.active_version)(
            assemblyRoot=child.component,
            )
        for st in childterms:
            sourceterms.append(SourceTerm.from_placement(st, assembly, child))
    # remove any that didn't match
    query = SourceTerm.objects(version_tags=assembly.active_version,
                               assemblyRoot=assembly)
    if component:
        query = query(assemblyPath__0__component=component)
    query(id__nin=[st.id for st in sourceterms]).delete()

    for parent in assembly.find_parents():
        update_assembly(sender=None, document=parent, component=assembly)


def update_placement(sender, document, **kwargs):
    # this doesn't work!!
    placement = document
    return update_assembly(sender=None, document=placement.parent,
                           placement=placement)

def update_emissionspec(sender, document, **kwargs):
    spec = document
    # find all components that own a reference to us and call update_component
    for component in Component.objects(version_tags=spec.active_version,
                                       reference_specs=spec):
        update_component(sender=None, document=component)

def update_hiteff(sender, document, **kwargs):
    hiteff = document
    # reverse the usual hiteff query to find all SourceTerms that would match
    terms = SourceTerm.select_version(hiteff.active_version)(
        hiteffs_auto=True,
        source__name=hiteff.source,
        location=hiteff.location,
        distribution=hiteff.distribution)
    for st in terms:
        st.hiteffs.append(hiteff)
        st.save()


mongoengine.signals.post_save.connect(update_component, sender=Component)
mongoengine.signals.post_save.connect(update_assembly, sender=Assembly)
mongoengine.signals.post_save.connect(update_emissionspec,
                                      sender=EmissionSpec)
mongoengine.signals.post_save.connect(update_hiteff, sender=HitEfficiency)
