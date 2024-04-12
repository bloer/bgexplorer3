from functools import cached_property, reduce
import operator
from mongoengine import (ListField, EmbeddedDocumentField, FloatField,
                         StringField, BooleanField, MapField, Document,
                         SortedListField, CASCADE, PULL, ReferenceField,
                         EmbeddedDocumentListField, ObjectIdField,
                         ValidationError)
from .verdoc import VersionedDocument, VersionedReferenceField, VersionedQuerySet
from .component import Component, Placement, Assembly
from .emissionspec import EmissionSpec, EmissionSource, Multiplier
from .fields import QuantityField, UncertainQuantityField, HistogramField
from .hiteff import HitEfficiency
from .isotope import concentration_to_rate
from .common import units, addnone, multnone
from . import settings
from typing import List, Optional, Union
from itertools import chain
import logging
log = logging.getLogger(__name__)


class SourceTerm(VersionedDocument):
    # these fields are keys used for finding SourceTerms in the db
    assemblyRoot = VersionedReferenceField(Component, required=True,
                                           reverse_delete_rule=CASCADE)
    assemblyPath = EmbeddedDocumentListField(Placement)
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
    livetimes = ListField(QuantityField(units='day', allownone=True,
                                        convert=True))
    # partially denormalized info
    # emissionrate = UncertainQuantityField(required=True)
    componentName = StringField()
    assemblyPathStr = StringField()
    material = StringField()
    # make queries against placement path much easier to assemble
    placement_ids = ListField(ObjectIdField())

    meta = {'indexes': ['assemblyPath', 'source.id', 'hiteffs',
                        'assemblyPath.component', 'placement_ids']}

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
        self.placement_ids = [placement.id for placement in self.assemblyPath]
        # TODO: should these be cached properties rather than set by clean?
        # location is set by the component or placement closest to the leaf
        for placement in reversed(self.assemblyPath):
            self.location = (placement.component.location or
                             placement.location)
            if self.location:
                break
        self.location = self.location or self.assemblyRoot.location
        self.distribution = \
            self.source.multiplier.determine_distribution(self.component)
        self.weight = reduce(operator.mul,
                             (p.weight for p in self.assemblyPath), 1)
        self.rate_multiplier = self.source.multiplier.getvalue(self.component)
        # default to componentName for location queries if location isn't set
        # TODO: should use root rather than component?
        self.componentName = self.component.name
        self.assemblyPathStr = '/'.join([self.assemblyRoot.name] +
                                        [p.name for p in self.assemblyPath])
        self.material = self.component.material
        # clear cached properties1
        try:
            del self.emissionrate
        except AttributeError:
            pass
        self.livetimes = [hit.get_livetime(self.emissionrate)
                          for hit in self.hiteffs]

    @property
    def hiteffs_query(self) -> VersionedQuerySet:
        hits = HitEfficiency.select_version(self.active_version)(
            source=self.source.name,
            location=self.location or self.componentName,
            material__in=(None, self.material),
            )
        if settings.get_settings(self.active_version).hiteffdbconfig\
                .query_distribution:
            hits = hits(distribution=self.distribution)
        return hits

    def find_hiteffs(self, replace: bool = True) -> List[HitEfficiency]:
        hiteffs = list(self.hiteffs_query.exclude('spectra'))

        if hiteffs and replace:
            self.hiteffs = hiteffs
            self.hiteffs_auto = True
            self.save()
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
                                   spec=self.spec,
                                   __raw__={'placement_ids':
                                            {'$eq': self.placement_ids}},
                                   )
        update_dict = dict(set_on_insert__id=self.id,
                           set_on_insert__original_id=self.original_id,
                           set_on_insert__version_tags=[self.active_version],
                           set_on_insert__assemblyRoot=self.assemblyRoot,
                           set_on_insert__assemblyPath=self.assemblyPath,
                           set_on_insert__spec=self.spec,
                           set_on_insert__placement_ids=self.placement_ids,
                           set__source=self.source,
                           set__location=self.location,
                           set__distribution=self.distribution,
                           set__weight=self.weight,
                           set__rate_multiplier=self.rate_multiplier,
                           set__componentName=self.component.name,
                           set__assemblyPathStr=self.assemblyPathStr,
                           set__material=self.material,
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


def find_sourceterms(obj: Union[Component, EmissionSpec, EmissionSource],
                     relativeto: Optional[Assembly] = None,
                     active_version: Optional[str] = None,
                     ) -> VersionedQuerySet:
    """ Find all SourceTerms for the given object """
    active_version = active_version or obj.active_version
    query = SourceTerm.select_version(active_version)
    if relativeto is not None:
        query = query(assemblyRoot=relativeto)
    if isinstance(obj, Component):
        if relativeto is not None and relativeto is not obj:
            query = query(assemblyPath__component=obj)
        else:
            query = query(assemblyRoot=obj)
    elif isinstance(obj, EmissionSpec):
        query = query(spec=obj)
    elif isinstance(obj, EmissionSource):
        query = query(source__id=obj.id)
    return query


class CalculatedResults(Document):
    """ Cache normalized HitEff results """
    # TODO: should this be a versioned document? it's acting as a cache
    # TODO: how to make sure there are no scalars/rois collisions?
    scalars = MapField(UncertainQuantityField(allownone=True),
                       required=False, default=dict)
    spectra = MapField(HistogramField(allownone=True),
                       required=False, default=dict)
    sources = SortedListField(ReferenceField(SourceTerm,
                                             reverse_delete_rule=CASCADE))
    meta = {'indexes': ['sources']}

    @classmethod
    def for_object(cls, obj, relativeto: Optional[Assembly] = None,
                   active_version: Optional[str] = None,
                   save: bool = False, save_intermediate: bool = False,
                   ) -> 'CalculatedResults':
        return cls.from_sourceterms(find_sourceterms(obj, relativeto,
                                                     active_version),
                                    save=save,
                                    save_intermediate=save_intermediate)

    @classmethod
    def for_object(cls, obj, relativeto: Optional[Assembly] = None,
                   save: bool = True, save_intermediate: bool = False):
        sts = find_sourceterms(obj=obj, relativeto=relativeto)
        return cls.from_sourceterms(sts, save=save,
                                    save_intermediate=save_intermediate)

    @classmethod
    def for_component(cls, component: Component,
                      relativeto: Optional[Assembly] = None,
                      save: bool = False, save_intermediate: bool = False):
        """ Alias for for_object. To be deprecated """
        return cls.for_object(component, relativeto, save, save_intermediate)

    def ito_reduced_units(self):
        for v in self.scalars.values():
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
        if other is None:
            return self
        elif not isinstance(other, CalculatedResults):
            raise TypeError('CalculatedResults can only add with others')
        # TODO: some checks to makes sure we're not duplicating sources
        # TODO: check active_version
        result = CalculatedResults(sources=self.sources + other.sources)
        for key in set(self.scalars).union(other.scalars):
            result.scalars[key] = addnone(self.scalars.get(key),
                                          other.scalars.get(key))
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
        return cls(scalars={key: multnone(val,  erate) for key, val in
                            chain(hiteff.scalars.items(), hiteff.rois.items())
                            if val is not None},
                   spectra={key: multnone(val, erate) for key, val in
                            hiteff.spectra.items() if val is not None},
                   ).ito_reduced_units()

    @classmethod
    def from_db(cls, sourceterms: List[SourceTerm]):
        ids = list(sorted(st.id for st in sourceterms))
        # can we do this with a match query?
        return cls.objects(__raw__={'sources': {'$eq': ids}}).first()

    @classmethod
    def from_sourceterm(cls, sourceterm: SourceTerm, allowcache: bool = True,
                        save: bool = False,
                        ) -> 'CalculatedResults':
        """ Calculalte all results for a single SourceTerm """
        if not sourceterm.hiteffs:
            return None
        if allowcache and (result := cls.from_db([sourceterm])) is not None:
            return result
        result = sum((cls._from_hiteff(sourceterm, h)
                      for h in sourceterm.hiteffs),
                     cls(sources=[sourceterm]))
        try:
            result.ito_reduced_units()
        except AttributeError:
            pass
        if save:
            result.save()
        return result

    @classmethod
    def from_sourceterms(cls, sourceterms: List[SourceTerm],
                         allowcache: bool = True, save: bool = False,
                         save_intermediate: bool = False,
                         ) -> 'CalculatedResults':
        if allowcache and (result := cls.from_db(sourceterms)) is not None:
            return result
        # even if subs are cached, re-calculate to pick up correlations
        result = sum((cls.from_sourceterm(st, allowcache=False,
                                          save=save_intermediate)
                      for st in sourceterms if st.hiteffs),
                     cls())
        if not result.sources:
            return None
        try:
            result.ito_reduced_units()
        except AttributeError:
            pass
        if save:
            result.save()
        return result
