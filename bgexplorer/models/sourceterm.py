from functools import cached_property, reduce
import operator
from mongoengine import (ListField, EmbeddedDocumentField, FloatField,
                         StringField, BooleanField, MapField, Document,
                         SortedListField, CASCADE, PULL, ReferenceField,
                         EmbeddedDocumentListField, ObjectIdField,
                         ValidationError)
from .verdoc import (VersionedDocument, VersionedReferenceField,
                     VersionedQuerySet, VersionedListField,
                     VersionedEmbeddedDocumentListField)
from .verdoc import ref_id as _ref_id
from .component import Component, Placement, Assembly
from .emissionspec import EmissionSpec, EmissionSource, Multiplier
from .fields import (QuantityField, UncertainQuantityField, HistogramField,
                     RawDictField)
from .asymmetric import AsymmetricUncertainty, LinearExpression
from .histogram import Histogram
from .hiteff import HitEfficiency
from .isotope import concentration_to_rate
from .common import units, addnone, multnone
from . import settings
from typing import List, Optional, Union, Dict, Tuple, Iterable
from collections import defaultdict, OrderedDict
from bson import ObjectId
from itertools import chain
import logging
import threading
log = logging.getLogger(__name__)


class SourceTerm(VersionedDocument):
    # these fields are keys used for finding SourceTerms in the db
    assemblyRoot = VersionedReferenceField(Component, required=True,
                                           reverse_delete_rule=CASCADE)
    assemblyPath = VersionedEmbeddedDocumentListField(Placement)
    source = EmbeddedDocumentField(EmissionSource, required=True)
    spec = VersionedReferenceField(EmissionSpec, required=True,
                                   reverse_delete_rule=CASCADE)
    # These are used to find hiteffs
    location = StringField()
    location_auto = BooleanField(default=True)
    # describes which component, placement, or override set location
    location_origin = StringField()
    # used to calculate results
    weight = FloatField(default=1)
    rate_multiplier = QuantityField(default=1)
    hiteffs = VersionedListField(VersionedReferenceField(
        HitEfficiency, reverse_delete_rule=PULL))
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
        if self.source.multiplier is Multiplier.mass and rate.dimensionless:
            rate = concentration_to_rate(self.source.name, rate)
        return rate * self.weight * self.rate_multiplier


    def set_location(self):
        """ Update the `location` attribute if it is auto """
        if self.location_auto:
            self.location, self.location_origin = self.resolve_location()
        return self.location

    def resolve_location(self) -> Tuple[str, str]:
        """ Find the location to match HitEfficiencies against, and a
        description of where it came from.

        The component or placement closest to the leaf wins. At each level,
        a component's location_overrides for our spec/source come before its
        location, and its parent's overrides for that placement before the
        placement's location. Falls back to the component name.
        """
        name = self.source.name
        path = self.assemblyPath
        parents = [self.assemblyRoot] + [p.component for p in path[:-1]]
        for placement, parent in zip(reversed(path), reversed(parents)):
            component = placement.component
            if found := component.find_location_override(self.spec, name):
                return found.location, f"override on {component.name}"
            if component.location:
                return component.location, f"component {component.name}"
            if found := parent.find_location_override(self.spec, name,
                                                      placement.id):
                return (found.location,
                        f"override on {parent.name} for {placement.name}")
            if placement.location:
                return (placement.location,
                        f"placement {parent.name}/{placement.name}")
        root = self.assemblyRoot
        if found := root.find_location_override(self.spec, name):
            return found.location, f"override on {root.name}"
        if root.location:
            return root.location, f"component {root.name}"
        return self.component.name, "component name"

    @property
    def location_overridden(self) -> bool:
        return (not self.location_auto or
                (self.location_origin or '').startswith('override'))


    def clean(self):
        super().clean()
        self.clear_results()
        self.placement_ids = [placement.id for placement in self.assemblyPath]
        # TODO: should these be cached properties rather than set by clean?
        self.set_location()
        self.weight = reduce(operator.mul,
                             (p.weight for p in self.assemblyPath), 1)
        self.rate_multiplier = self.source.multiplier.getvalue(self.component)
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
            location=self.location,
            material__in=(None, self.material),
            )
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
                           set_on_insert__spec=self.spec,
                           set_on_insert__placement_ids=self.placement_ids,
                           # placement ids are in the query, but copied
                           # labels, weights, etc may have changed
                           set__assemblyPath=self.assemblyPath,
                           set__source=self.source,
                           set__location=self.location,
                           set__location_origin=self.location_origin,
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


def find_sourceterms(obj: Union[Component, EmissionSpec, EmissionSource, HitEfficiency],
                     relativeto: Optional[Assembly] = None,
                     active_version: Optional[str] = None,
                     ) -> VersionedQuerySet:
    """ Find all SourceTerms for the given object """
    active_version = active_version or obj.active_version
    query = SourceTerm.select_version(active_version)
    if relativeto is not None:
        query = query(assemblyRoot=relativeto)
    if isinstance(obj, Component):
        if (relativeto is not None and
                relativeto.original_id != obj.original_id):
            query = query(assemblyPath__component=obj)
        else:
            query = query(assemblyRoot=obj)
    elif isinstance(obj, EmissionSpec):
        query = query(spec=obj)
    elif isinstance(obj, EmissionSource):
        query = query(source__id=obj.id)
    elif isinstance(obj, HitEfficiency):
        query = query(hiteffs=obj)
    return query


class CalculatedResults(Document):
    """ Cache normalized HitEff results """
    # TODO: should this be a versioned document? it's acting as a cache
    # TODO: how to make sure there are no scalars/rois collisions?
    scalars = MapField(UncertainQuantityField(allownone=True,
                                              expressions='separate'),
                       required=False, default=dict)
    spectra = MapField(HistogramField(allownone=True,
                                      expressions='separate'),
                       required=False, default=dict)
    # the expressions of the values, which are big for spectra, so that
    # the values can be loaded without them
    scalars_expr = RawDictField()
    spectra_expr = RawDictField()
    sources = SortedListField(ReferenceField(SourceTerm,
                                             reverse_delete_rule=CASCADE))
    meta = {'indexes': ['sources']}
    EXPR_FIELDS = ('scalars_expr', 'spectra_expr')

    def clean(self):
        super().clean()
        for name, values, get in (('scalars_expr', self.scalars, _magnitude),
                                  ('spectra_expr', self.spectra,
                                   _hist_magnitude)):
            exprs = dict(getattr(self, name) or {})
            for key, value in values.items():
                au = get(value)
                # values loaded without theirs keep what's stored
                if au is not None and au.correlations_loaded:
                    expr = au.serialize_expression()
                    if expr is None:
                        exprs.pop(key, None)
                    else:
                        exprs[key] = expr
            setattr(self, name, exprs)

    def load_correlations(self) -> 'CalculatedResults':
        """ Give values loaded without their correlations the expressions
        stored for them. Returns self
        """
        for values, exprs, get in ((self.scalars, self.scalars_expr,
                                    _magnitude),
                                   (self.spectra, self.spectra_expr,
                                    _hist_magnitude)):
            for key, value in values.items():
                au = get(value)
                if au is None or au.correlations_loaded or key not in exprs:
                    continue
                correlated = AsymmetricUncertainty(
                    au.mode, au.s0, au.s1,
                    expression=LinearExpression.from_terms(exprs[key]))
                if isinstance(value, Histogram):
                    value.hist = units.Quantity(correlated, value.hist.u)
                else:
                    values[key] = units.Quantity(correlated, value.u)
        return self

    @classmethod
    def for_object(cls, obj, relativeto: Optional[Assembly] = None,
                   save: bool = True, spectra: bool = True,
                   cache: bool = True, correlations: bool = True):
        """ Calculate results for all SourceTerms of `obj`, optionally
        only the part in assembly `relativeto`. If `cache`, results are
        kept in memory until any data in the version changes.

        If not `correlations`, saved results are loaded without their
        correlations, which is faster when they're only shown.
        """
        def calculate():
            sts = find_sourceterms(obj=obj, relativeto=relativeto)
            return cls.from_sourceterms(sts, save=save, spectra=spectra,
                                        correlations=correlations)
        if not cache:
            return calculate()
        key = _cache_key(obj, 'object', type(obj).__name__, _ref_id(obj),
                         relativeto and _ref_id(relativeto), spectra,
                         correlations)
        return _cached(key, calculate)

    @classmethod
    def for_component(cls, component: Component,
                      relativeto: Optional[Assembly] = None,
                      save: bool = False, spectra: bool = True):
        """ Alias for for_object. To be deprecated """
        return cls.for_object(component, relativeto, save, spectra)

    @classmethod
    def for_tree(cls, root: Component, spectra: bool = False,
                 cache: bool = True) -> Dict[ObjectId, 'CalculatedResults']:
        """ Calculate results for `root` and, relative to `root`, every
        component placed anywhere below it. This is equivalent to calling
        `for_object(component, relativeto=root)` for each of them, but
        loads everything once.

        Returns a dict keyed by component original_id. Results aren't
        cached in the database, but if `cache` they are kept in memory
        until any data in the version changes.
        """
        if cache:
            key = _cache_key(root, 'tree', root.original_id, spectra)
            return _cached(key, lambda: cls.for_tree(root, spectra, False))
        sourceterms = list(find_sourceterms(root))
        hiteffs = load_hiteffs(sourceterms, spectra)
        parts = {st.id: cls._calculate(st, hiteffs, spectra)
                 for st in sourceterms}
        groups = defaultdict(list)
        groups[root.original_id] = sourceterms
        for st in sourceterms:
            # a component may be placed more than once in the same path
            for cid in {_ref_id(p._data.get('component'))
                        for p in st._data.get('assemblyPath') or []}:
                groups[cid].append(st)
        return {cid: cls._sum(sts, (parts[st.id] for st in sts))
                for cid, sts in groups.items()}

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

    @staticmethod
    def _calculate(sourceterm: SourceTerm,
                   hiteffs: Dict[ObjectId, HitEfficiency],
                   spectra: bool = True) -> Optional[Tuple[dict, dict]]:
        """ Multiply the sourceterm's emission rate by each of its
        hiteffs, which are looked up in `hiteffs` (see `load_hiteffs`).
        Returns (scalars, spectra) dicts, or None if there are no hiteffs.
        Units aren't reduced.
        """
        scalars, hists = {}, {}
        found = False
        for ref in sourceterm._data.get('hiteffs') or []:
            hiteff = hiteffs.get(_ref_id(ref))
            if hiteff is None:
                if not isinstance(ref, HitEfficiency):
                    log.warning(f"HitEfficiency {_ref_id(ref)} for "
                                f"SourceTerm {sourceterm.id} not found")
                    continue
                hiteff = ref
            found = True
            erate = sourceterm.emissionrate
            if not hiteff.norm.check(erate):
                raise units.DimensionalityError(
                    "incompatible emissionrate units")
            for key, val in chain(hiteff.scalars.items(),
                                  hiteff.rois.items()):
                if val is not None:
                    scalars[key] = addnone(scalars.get(key),
                                           multnone(val, erate))
            if spectra:
                for key, val in hiteff.spectra.items():
                    if val is not None:
                        hists[key] = addnone(hists.get(key),
                                             multnone(val, erate))
        return (scalars, hists) if found else None

    @classmethod
    def _sum(cls, sourceterms: List[SourceTerm],
             parts: Iterable[Optional[Tuple[dict, dict]]]
             ) -> Optional['CalculatedResults']:
        """ Add up the output of `_calculate` for each of `sourceterms`.
        Returns None if none of them have hiteffs
        """
        scalars, hists = {}, {}
        found = False
        for part in parts:
            if part is None:
                continue
            found = True
            for key, val in part[0].items():
                scalars[key] = addnone(scalars.get(key), val)
            for key, val in part[1].items():
                hists[key] = addnone(hists.get(key), val)
        if not found:
            return None
        return cls(scalars=scalars, spectra=hists,
                   sources=list(sourceterms)).ito_reduced_units()

    @classmethod
    def from_db(cls, sourceterms: List[SourceTerm],
                correlations: bool = True
                ) -> Optional['CalculatedResults']:
        """ The saved results for exactly `sourceterms`, if any. If not
        `correlations`, their values are loaded without them, see
        `AsymmetricUncertainty.without_correlations`
        """
        ids = list(sorted(st.id for st in sourceterms))
        # can we do this with a match query?
        query = cls.objects(__raw__={'sources': {'$eq': ids}})
        if not correlations:
            return query.exclude(*cls.EXPR_FIELDS).first()
        result = query.first()
        return result and result.load_correlations()

    @classmethod
    def from_sourceterm(cls, sourceterm: SourceTerm, allowcache: bool = True,
                        save: bool = False, spectra: bool = True,
                        ) -> 'CalculatedResults':
        """ Calculalte all results for a single SourceTerm """
        return cls.from_sourceterms([sourceterm], allowcache=allowcache,
                                    save=save, spectra=spectra)

    @classmethod
    def from_sourceterms(cls, sourceterms: Iterable[SourceTerm],
                         allowcache: bool = True, save: bool = False,
                         spectra: bool = True, correlations: bool = True,
                         ) -> Optional['CalculatedResults']:
        """ Calculate the sum of results for all `sourceterms`. Returns None
        if none of them have hiteffs.

        If `allowcache`, first look for a saved result in the database. If
        `save`, save the result. If not `spectra`, only calculate scalars;
        these partial results are never saved. If not `correlations`, saved
        results are loaded without them.
        """
        sourceterms = list(sourceterms)
        if allowcache and (result := cls.from_db(sourceterms, correlations)
                           ) is not None:
            return result
        hiteffs = load_hiteffs(sourceterms, spectra)
        result = cls._sum(sourceterms, (cls._calculate(st, hiteffs, spectra)
                                        for st in sourceterms))
        if result is not None and save and spectra:
            result.save()
        return result


class ResultsCache:
    """ In-memory LRU cache of calculated results. Keys start with the
    version's cache token, which changes whenever any data in the version
    changes, so entries never need invalidating and it is safe with multiple
    processes. Results are shared, so must not be modified
    """
    instances = []

    def __init__(self, size: int):
        self.size = size
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        ResultsCache.instances.append(self)

    def get(self, key: Optional[tuple], calculate):
        """ Return the cached result for `key`, or else calculate and cache
        it. `key` None isn't cached
        """
        if key is None:
            return calculate()
        with self._lock:
            result = self._entries.get(key, _MISSING)
            if result is not _MISSING:
                self._entries.move_to_end(key)
                return result
        # if the data change while calculating, the result is stored with
        # the old token and never used
        result = calculate()
        with self._lock:
            self._entries[key] = result
            while len(self._entries) > self.size:
                self._entries.popitem(last=False)
        return result

    def clear(self):
        with self._lock:
            self._entries.clear()

    def __len__(self):
        return len(self._entries)


RESULTS_CACHE_SIZE = 128
_MISSING = object()
_results_cache = ResultsCache(RESULTS_CACHE_SIZE)


def _magnitude(value) -> Optional[AsymmetricUncertainty]:
    m = getattr(value, 'm', None)
    return m if isinstance(m, AsymmetricUncertainty) else None


def _hist_magnitude(value) -> Optional[AsymmetricUncertainty]:
    return _magnitude(getattr(value, 'hist', None))


def _cache_key(obj, *args) -> Optional[tuple]:
    """ Cache key for results of `obj`, or None if they can't be cached """
    version = getattr(obj, 'active_version', None)
    token = version and settings.get_cache_token(version)
    return (token,) + args if token is not None else None


def _cached(key: Optional[tuple], calculate):
    """ Return the result for `key` from the shared results cache """
    return _results_cache.get(key, calculate)


def clear_results_cache():
    """ Empty every ResultsCache """
    for cache in ResultsCache.instances:
        cache.clear()


def load_hiteffs(sourceterms: Iterable[SourceTerm], spectra: bool = True
                 ) -> Dict[ObjectId, HitEfficiency]:
    """ Load all HitEfficiencies referenced by `sourceterms` with one query
    per version, rather than dereferencing each sourceterm's list. If not
    `spectra`, don't load spectra.

    Returns a dict keyed by original_id
    """
    ids = defaultdict(set)
    for st in sourceterms:
        ids[st.active_version].update(
            _ref_id(ref) for ref in st._data.get('hiteffs') or [])
    result = {}
    for version, versionids in ids.items():
        query = HitEfficiency.select_version(version)(
            original_id__in=list(versionids))
        if not spectra:
            query = query.exclude('spectra')
        result.update((hiteff.original_id, hiteff) for hiteff in query)
    return result
