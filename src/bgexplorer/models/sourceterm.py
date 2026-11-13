from functools import cached_property, reduce
import operator
import hashlib
import datetime
from functools import cache
from bson import json_util
from pymongo import InsertOne, UpdateOne
from mongoengine import (ListField, EmbeddedDocumentField, FloatField,
                         StringField, BooleanField, MapField, Document,
                         SortedListField, CASCADE, PULL, ReferenceField,
                         EmbeddedDocumentListField, ObjectIdField,
                         ValidationError, IntField)
from ..versioning.verdoc import (VersionedDocument, VersionedReferenceField,
                     VersionedQuerySet, VersionedListField,
                     VersionedEmbeddedDocumentListField)
from ..versioning.verdoc import ref_id as _ref_id
from ..versioning.verdoc import (IDENTITY_FIELDS, check_writable, current_user_name)
from ..versioning import record
from .component import (Component, Placement, Assembly, ComponentSpec,
                        LocationMode)
from .emissionspec import EmissionSpec, EmissionSource, Multiplier
from .fields import (QuantityField, UncertainQuantityField, HistogramField,
                     RawDictField)
from .asymmetric import AsymmetricUncertainty, LinearExpression
from .histogram import Histogram
from .hiteff import HitEfficiency
from .common import units, addnone, multnone, Quantity
from .asymmetric import sum_values
from . import settings
from typing import (List, Optional, Union, Dict, Tuple, Iterable,
                    NamedTuple, Sequence, Set)
from collections import defaultdict, OrderedDict
from bson import ObjectId
import logging
import os
import sys
import threading
from types import SimpleNamespace
import numpy as np
log = logging.getLogger(__name__)
NO_MATERIAL = '(no material)'
NO_CATEGORY = '(uncategorized)'


class SourceTerm(VersionedDocument):
    VERSION_DERIVED = True

    @classmethod
    def find_dangling(cls, version_tag):
        """ SourceTerms whose component or spec is missing from the version """
        def ids(doc_cls):
            return set(doc_cls._get_collection().distinct(
                'original_id', {'version_tags': version_tag}))
        components, specs = ids(Component), ids(EmissionSpec)
        bad = []
        for st in cls._get_collection().find({'version_tags': version_tag},
                                             {'assemblyRoot': 1, 'spec': 1}):
            root, spec = _ref_id(st.get('assemblyRoot')), _ref_id(st.get('spec'))
            if (root is None or root not in components
                    or (spec is not None and spec not in specs)):
                bad.append(st['_id'])
        return bad
    VERSION_TOUCH_TERMS_ONLY = True

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
    confidence_rating = IntField(choices=[1, 2, 3, 4, 5])
    # make queries against placement path much easier to assemble
    placement_ids = ListField(ObjectIdField())
    # identifies everything that determines this term's calculated values
    # other than the HitEfficiencies' content, see `content_stamp`
    stamp = StringField()

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
        return (self.source.standardized_rate * self.weight
                * self.rate_multiplier).to(self.source.multiplier.norm.units)


    def group_keys(self) -> dict:
        """ What this term is grouped by in a budget breakdown. The component
        key is the path of placement ids (as strings) from the assembly root
        """
        category = self.source.category
        return dict(
            component=tuple(str(pid) for pid in self.placement_ids or []),
            isotope=self.source.name,
            # a component without a material is listed by name
            material=self.material or self.componentName or NO_MATERIAL,
            category=category.value if category else NO_CATEGORY)

    def set_location(self):
        """ Update the `location` attribute if it is auto """
        if self.location_auto:
            self.location, self.location_origin = self.resolve_location()
        return self.location

    def inherited_location(self) -> Tuple[str, str]:
        """ The location from the component or placement closest to the leaf
        that sets one, ignoring the spec's own setting, and a description of
        where it came from. Falls back to the component name.
        """
        path = self.assemblyPath
        parents = [self.assemblyRoot] + [p.component for p in path[:-1]]
        for placement, parent in zip(reversed(path), reversed(parents)):
            component = placement.component
            if component.location:
                return component.location, f"component {component.name}"
            if placement.location:
                return (placement.location,
                        f"placement {parent.name}/{placement.name}")
        root = self.assemblyRoot
        if root.location:
            return root.location, f"component {root.name}"
        return self.component.name, "component name"

    @property
    def component_spec(self) -> Optional[ComponentSpec]:
        """ The settings for our spec on our component """
        return self.component.find_spec_entry(self.spec)

    def resolve_location(self) -> Tuple[str, str]:
        """ Find the location to match HitEfficiencies against, and a
        description of where it came from.

        The component or placement closest to the leaf wins, then the
        location set for the spec on the component replaces it, or is added
        to its start or end.
        """
        entry = self.component_spec
        if entry is None or not entry.location:
            return self.inherited_location()
        component = self.component
        if entry.location_mode is LocationMode.replace:
            return entry.location, f"override on {component.name}"
        base, origin = self.inherited_location()
        return (entry.apply(base),
                f"override on {component.name}"
                f" ({entry.location_mode.value} to {origin})")

    @property
    def location_overridden(self) -> bool:
        return (not self.location_auto or
                (self.location_origin or '').startswith('override'))


    def content_stamp(self) -> str:
        """ A digest of everything about this SourceTerm that its values
        and keys in a budget depend on, besides the content of its
        HitEfficiencies. The same for SourceTerms with the same content
        """
        # the stored _id, which is what a reference that isn't dereferenced
        # yet has (ref_id gives original_id for documents)
        hiteffs = [str(h.id) for h in self._data.get('hiteffs') or []]
        multiplier = self.rate_multiplier
        if isinstance(multiplier, Quantity):
            multiplier = (float(multiplier.m), str(multiplier.u))
        else:
            multiplier = (float(multiplier), '')
        content = (self.source.to_mongo(), float(self.weight), multiplier,
                   self.material,
                   self.componentName, self.confidence_rating,
                   [str(pid) for pid in self.placement_ids], hiteffs)
        return hashlib.sha1(repr(content).encode()).hexdigest()[:20]

    def derive(self) -> None:
        """ Set the fields that follow from the placement path and its
        components. Doesn't touch the database or the hit efficiencies.
        """
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
        entry = self.component_spec
        self.confidence_rating = (
            entry.confidence_rating if entry and entry.confidence_rating
            else self.component.confidence_rating)
        # clear cached properties
        try:
            del self.emissionrate
        except AttributeError:
            pass

    def clean(self):
        super().clean()
        self.derive()
        self.livetimes = [hit.get_livetime(self.emissionrate)
                          for hit in self.hiteffs]
        self.stamp = self.content_stamp()

    @property
    def natural_key(self) -> tuple:
        """ What identifies this SourceTerm within its assembly root and
        version. Needs `derive` to have set the placement ids
        """
        return (self.source.id, _ref_id(self.spec), tuple(self.placement_ids))


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


_LISTED_FIELDS = ('original_id', 'version_tags', 'assemblyRoot',
                  'assemblyPath', 'spec', 'source', 'weight',
                  'rate_multiplier', 'location', 'location_auto',
                  'location_origin', 'hiteffs', 'hiteffs_auto', 'livetimes')


def listed_sourceterms(query: VersionedQuerySet) -> list:
    """ The SourceTerms of `query` for a listing, as plain objects with the
    components, specs and hit efficiencies they refer to already loaded:
    one query for each kind, instead of several for each SourceTerm. Ones
    with a reference that is missing from the version are skipped.
    """
    version = query.active_version
    terms = list(query.only(*_LISTED_FIELDS))
    # read the stored ids, which doesn't load what they refer to
    paths = [list(st._data.get('assemblyPath') or []) for st in terms]
    comp_ids = {_ref_id(st._data['assemblyRoot']) for st in terms}
    comp_ids.update(_ref_id(p._data['component']) for path in paths
                    for p in path)
    spec_ids = {_ref_id(st._data['spec']) for st in terms}
    hit_ids = {_ref_id(h) for st in terms
               for h in st._data.get('hiteffs') or []}

    def load(doc_cls, ids, *fields):
        found = doc_cls.select_version(version)(original_id__in=list(ids))
        return {doc.original_id: doc for doc in
                found.only('original_id', 'version_tags', *fields)}
    comps = load(Component, comp_ids, 'name')
    specs = load(EmissionSpec, spec_ids, 'name')
    hiteffs = load(HitEfficiency, hit_ids, 'source', 'location')

    rows = []
    for st, path in zip(terms, paths):
        root = comps.get(_ref_id(st._data['assemblyRoot']))
        spec = specs.get(_ref_id(st._data['spec']))
        placed = [comps.get(_ref_id(p._data['component'])) for p in path]
        hits = [hiteffs.get(_ref_id(h)) for h in st._data.get('hiteffs') or []]
        if None in (root, spec, *placed, *hits):
            log.warning("Skipping SourceTerm %s with a missing reference",
                        st.id)
            continue
        rows.append(SimpleNamespace(
            id=st.id, original_id=st.original_id, assemblyRoot=root,
            assemblyPath=[SimpleNamespace(component=comp, weight=p.weight,
                                          name=p.label or comp.name)
                          for p, comp in zip(path, placed)],
            spec=spec, source=st.source, emissionrate=st.emissionrate,
            location=st.location, location_origin=st.location_origin,
            location_overridden=st.location_overridden,
            hiteffs_auto=st.hiteffs_auto, hiteffs=hits,
            livetimes=st.livetimes))
    return rows


class _Flight:
    """ A calculation in progress, which other threads can wait for """
    def __init__(self):
        self.done = threading.Event()
        self.thread = threading.get_ident()


def estimate_bytes(value) -> int:
    """ A rough estimate of the memory used by a cached result: the arrays it
    holds and the containers around them. Objects with their own `nbytes`
    report it
    """
    if isinstance(value, np.ndarray):
        return value.nbytes + 100
    nbytes = getattr(value, 'nbytes', None)
    if isinstance(nbytes, int):
        return nbytes
    if isinstance(value, dict):
        return sys.getsizeof(value) + sum(
            estimate_bytes(k) + estimate_bytes(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return sys.getsizeof(value) + sum(estimate_bytes(v) for v in value)
    if isinstance(value, AsymmetricUncertainty):
        return 200 + sum(estimate_bytes(a) for a in
                         (value._mode, value._s0, value._s1))
    if isinstance(value, Histogram):
        return 200 + estimate_bytes(value.hist) + estimate_bytes(
            value.bin_edges)
    if hasattr(value, 'magnitude'):
        return 100 + estimate_bytes(value.magnitude)
    return sys.getsizeof(value)


def cache_bytes(share: float) -> int:
    """ This share of the memory that each process may use for caches, which
    is `BGEXPLORER_CACHE_MB` (default 512) megabytes
    """
    try:
        megabytes = float(os.environ.get('BGEXPLORER_CACHE_MB', 512))
    except ValueError:
        megabytes = 512.
    return int(share * megabytes * 2**20)


class ResultsCache:
    """ In-memory LRU cache of calculated results, bounded by the number of
    entries and optionally by their size in bytes (as `sizeof` estimates).
    Keys start with the version's cache token, which changes whenever any data
    in the version changes, so entries never need invalidating and it is safe
    with multiple processes. Those of old tokens can never be used again, and
    are removed by `purge_token`. Results are shared, so must not be modified.

    Results that grow after they are cached (that have `nbytes`) are measured
    again whenever they are used. One that is bigger than all of `max_bytes`
    isn't kept.

    If several threads want the same missing key at once, one calculates it
    and the others wait for it, rather than all repeating a slow calculation.
    """
    instances = []

    def __init__(self, size: int, max_bytes: Optional[int] = None,
                 sizeof=estimate_bytes, by_token: bool = True,
                 name: str = ''):
        """ Keep at most `size` results, and `max_bytes` if that is given.
        If not `by_token`, keys don't start with a token
        """
        self.size = size
        self.max_bytes = max_bytes
        self.name = name
        self._sizeof = sizeof
        self._by_token = by_token
        self._entries = OrderedDict()
        self._sizes = {}
        self._nbytes = 0
        self._tokens = defaultdict(set)
        self._flights = {}
        self._lock = threading.Lock()
        ResultsCache.instances.append(self)

    def _store(self, key, result) -> None:
        """ Add or replace an entry. The lock must be held """
        self._remove(key)
        nbytes = self._sizeof(result)
        if self.max_bytes is not None and nbytes > self.max_bytes:
            return
        self._entries[key] = result
        self._sizes[key] = nbytes
        self._nbytes += nbytes
        if self._by_token:
            self._tokens[key[0]].add(key)

    def _remove(self, key) -> None:
        """ Drop an entry if there is one. The lock must be held """
        if self._entries.pop(key, _MISSING) is _MISSING:
            return
        self._nbytes -= self._sizes.pop(key)
        if self._by_token:
            keys = self._tokens.get(key[0])
            if keys is not None:
                keys.discard(key)
                if not keys:
                    del self._tokens[key[0]]

    def _evict(self) -> None:
        """ Drop the least recently used entries until it fits. The lock must
        be held
        """
        while self._entries and (
                len(self._entries) > self.size or
                (self.max_bytes is not None and self._nbytes > self.max_bytes)):
            self._remove(next(iter(self._entries)))

    def _hit(self, key, result) -> None:
        """ Note that an entry was used. The lock must be held """
        self._entries.move_to_end(key)
        if isinstance(getattr(result, 'nbytes', None), int):
            self._nbytes += result.nbytes - self._sizes[key]
            self._sizes[key] = result.nbytes
            self._evict()

    def get(self, key: Optional[tuple], calculate):
        """ Return the cached result for `key`, or else calculate and cache
        it. `key` None isn't cached
        """
        if key is None:
            return calculate()
        while True:
            with self._lock:
                result = self._entries.get(key, _MISSING)
                if result is not _MISSING:
                    self._hit(key, result)
                    return result
                flight = self._flights.get(key)
                if flight is None:
                    flight = self._flights[key] = _Flight()
                    break
            if flight.thread == threading.get_ident():
                # calculating this key needs this key
                return calculate()
            # if the calculation failed, try again ourselves
            flight.done.wait()
        # if the data change while calculating, the result is stored with
        # the old token and never used
        try:
            result = calculate()
            with self._lock:
                self._store(key, result)
                self._evict()
            return result
        finally:
            with self._lock:
                del self._flights[key]
            flight.done.set()

    def get_many(self, keys: Iterable) -> dict:
        """ The cached results of those of `keys` that have any, without
        calculating the rest
        """
        found = {}
        with self._lock:
            for key in keys:
                result = self._entries.get(key, _MISSING)
                if result is not _MISSING:
                    self._hit(key, result)
                    found[key] = result
        return found

    def put_many(self, items: Dict) -> None:
        """ Cache several results at once """
        with self._lock:
            for key, result in items.items():
                self._store(key, result)
            self._evict()

    def purge_token(self, token) -> int:
        """ Drop the entries of an old cache `token`. Returns how many """
        with self._lock:
            keys = list(self._tokens.get(token, ()))
            for key in keys:
                self._remove(key)
        return len(keys)

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._sizes.clear()
            self._tokens.clear()
            self._nbytes = 0

    def __len__(self):
        return len(self._entries)

    @property
    def nbytes(self) -> int:
        """ The estimated size of the results in the cache """
        return self._nbytes


RESULTS_CACHE_SIZE = 128
_MISSING = object()
_results_cache = ResultsCache(RESULTS_CACHE_SIZE, cache_bytes(.2),
                              name='results')
# the latest cache token seen for each version
_current_tokens = {}
_tokens_lock = threading.Lock()


def _note_token(version: str, token) -> None:
    """ When a version's cache token has changed, drop the results of the
    old one from every cache
    """
    with _tokens_lock:
        old = _current_tokens.get(version)
        if old == token:
            return
        _current_tokens[version] = token
    if old is not None:
        for cache in ResultsCache.instances:
            cache.purge_token(old)


def _cache_key(obj, *args) -> Optional[tuple]:
    """ Cache key for results of `obj`, or None if they can't be cached """
    version = getattr(obj, 'active_version', None)
    token = version and record.get_cache_token(version)
    if token is None:
        return None
    _note_token(version, token)
    return (token,) + args


def _cached(key: Optional[tuple], calculate):
    """ Return the result for `key` from the shared results cache """
    return _results_cache.get(key, calculate)


def clear_results_cache():
    """ Empty every ResultsCache """
    for cache in ResultsCache.instances:
        cache.clear()


def clear_calculation_cache() -> None:
    """ Empty the in-memory calculation caches, and drop the collection of
    results that older versions stored in the database
    """
    SourceTerm._get_db().drop_collection('calculated_results')
    clear_results_cache()


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


def check_version_units(version: str) -> List[str]:
    """ Audit the units of everything in `version`, for data that was written
    without being validated, such as an import or a merge. HitEfficiencies'
    scalars, spectra and ROIs without a display unit in the settings are
    given one, and every HitEfficiency and EmissionSource is checked as
    saving it would be. Returns a description of each problem found, which
    is empty if the units all agree.
    """
    vsettings = settings.get_settings(version, create=False)
    config = vsettings.hiteffdbconfig
    before = config.to_mongo()
    hiteffs = list(HitEfficiency.select_version(version))
    for hiteff in hiteffs:
        config.update_from(hiteff)
    if config.to_mongo() != before:
        # update rather than save: the version may be locked or read-only
        vsettings.update(set__hiteffdbconfig=config)
        settings.touch(version)

    problems = []
    for hiteff in hiteffs:
        try:
            hiteff.check_dbconfig(config)
        except ValidationError as e:
            problems.append(f"HitEfficiency {hiteff.id} ({hiteff}): "
                            f"{e.message}")
    for spec in EmissionSpec.select_version(version):
        for source in spec.sources:
            try:
                source.clean()
            except ValidationError as e:
                problems.append(f"EmissionSpec {spec.name}, source "
                                f"{source.name}: {e.message}")
    problems = list(dict.fromkeys(problems))
    for problem in problems:
        log.warning("Unit mismatch in version '%s': %s", version, problem)
    return problems


class HitEffIndex:
    """ The HitEfficiencies a batch of SourceTerms may match, loaded once
    without their spectra and shared between the SourceTerms, rather than
    querying for each one.
    """
    def __init__(self, version: str):
        self.version = version
        self._by_key = defaultdict(set)
        self._by_id = {}

    def get_key(self, doc: SourceTerm | HitEfficiency) -> tuple[str]:
        """ For SourceTerm or HitEfficiency, produce the key used to index and query """
        try:
            key = (doc.source.name, doc.location)
        except AttributeError:
            # this is a hiteff
            key = (doc.source, doc.location)
        return key

    def load(self, keys: Iterable[tuple[str]], ids: Iterable[ObjectId] = ()
             ) -> None:
        """ Load every HitEfficiency of the named `sources` for `match`, and
        those with the given original ids for `get`, if not already loaded
        """
        # TODO: this query will load all hiteffs with the same source, which
        #       could be hundreds of times more than we need.
        #       key the query on location, material, and norm too
        query = HitEfficiency.select_version(self.version).exclude('spectra')
        newkeys = set(keys) - set(self._by_key.keys())
        newkeyparts = list(zip(*newkeys))
        if newkeys:
            for hiteff in query(source__in=sorted(newkeyparts[0]),
                                location__in=sorted(newkeyparts[1])):
                self._by_id[hiteff.original_id] = hiteff
                self._by_key[self.get_key(hiteff)].add(hiteff)
        missing = set(ids) - self._by_id.keys()
        if missing:
            for hiteff in query(original_id__in=list(missing)):
                self._by_id[hiteff.original_id] = hiteff

    def match(self, sourceterm: SourceTerm) -> List[HitEfficiency]:
        """ The HitEfficiencies that apply to `sourceterm` (those of its source
        and location, with no material or its material), whose location and
        material must already be set
        """
        key = self.get_key(sourceterm)
        return [hiteff for hiteff in self._by_key.get(key, ())
                if (hiteff.material in (None, sourceterm.material)
                    and sourceterm.source.multiplier.norm == hiteff.norm)]

    def get(self, ids: Iterable[ObjectId]) -> List[HitEfficiency]:
        """ The loaded HitEfficiencies with these original ids """
        return [self._by_id[i] for i in ids if i in self._by_id]


def placed_sourceterms(assembly: Assembly, placements: Sequence[Placement],
                       scope: Sequence[Set[ObjectId]] = ()
                       ) -> List[SourceTerm]:
    """ Unsaved SourceTerms of `assembly` for each of the given `placements`
    of its children, made from the SourceTerms of the placed components. Only
    those whose path starts below `scope` (a list of sets of placement ids,
    one for each level of the path) are used. Their hit efficiencies are left
    for `sync_sourceterms` to find, except for manually set ones, which
    are carried up in `_manual_hiteffs`.
    """
    version = assembly.active_version
    roots = {p.component.original_id for p in placements}
    query = SourceTerm.objects(version_tags=version,
                               assemblyRoot__in=list(roots))
    if scope:
        query = query(__raw__={f'placement_ids.{i}': {'$in': list(ids)}
                               for i, ids in enumerate(scope)})
    raws = list(query.only('assemblyRoot', 'assemblyPath', 'source', 'spec',
                           'hiteffs_auto', 'hiteffs').as_pymongo())
    by_root = defaultdict(list)
    comp_ids = set(roots)
    spec_ids = set()
    for raw in raws:
        by_root[raw['assemblyRoot']].append(raw)
        comp_ids.update(p['component'] for p in raw.get('assemblyPath', ()))
        spec_ids.add(raw['spec'])
    comps = {c.original_id: c for c in Component.select_version(version)(
        original_id__in=list(comp_ids))}
    specs = {s.original_id: s for s in EmissionSpec.select_version(version)(
        original_id__in=list(spec_ids))}

    result = []
    for placement in placements:
        for raw in by_root[placement.component.original_id]:
            spec = specs.get(raw['spec'])
            below = [comps.get(p['component']) for p in
                     raw.get('assemblyPath', ())]
            if spec is None or None in below:
                log.warning("Skipping SourceTerm %s with a missing spec or "
                            "component", raw['_id'])
                continue
            path = [placement] + [
                Placement(id=p['id'], component=comp, weight=p.get('weight', 1),
                          label=p.get('label'), location=p.get('location'))
                for p, comp in zip(raw.get('assemblyPath', ()), below)]
            auto = raw.get('hiteffs_auto', True)
            sourceterm = SourceTerm(
                version_tag=version, assemblyRoot=assembly, assemblyPath=path,
                source=EmissionSource._from_son(raw['source']), spec=spec,
                hiteffs_auto=auto)
            if not auto:
                sourceterm._manual_hiteffs = list(raw.get('hiteffs', ()))
            result.append(sourceterm)
    return result


@cache
def _content_keys() -> frozenset:
    """ The database names of the fields `sync_sourceterms` compares and
    writes, which is all but the ones that identify and track the document
    """
    fields = {f.db_field for f in SourceTerm._fields.values()}
    return frozenset(fields - set(IDENTITY_FIELDS) - {'enteredby'})


def _digest(doc: dict, keys: frozenset) -> str:
    content = {k: doc[k] for k in keys if k in doc}
    return hashlib.sha1(json_util.dumps(content, sort_keys=True).encode()
                        ).hexdigest()


class _Stored(NamedTuple):
    """ What `sync_sourceterms` keeps of a SourceTerm already in the database
    """
    id: ObjectId
    original_id: ObjectId
    shared: bool
    digest: str
    # the original ids of the hiteffs if they were set manually
    manual: Optional[list]


class SyncResult(NamedTuple):
    inserted: int = 0
    updated: int = 0
    removed: int = 0

    @property
    def changed(self) -> bool:
        return any(self)


def sync_sourceterms(version: str, candidates: Iterable[SourceTerm],
                     existing: VersionedQuerySet) -> SyncResult:
    """ Make the SourceTerms in `existing` (a query for a subset of `version`)
    the same as the unsaved `candidates`, by writing only what changed.

    The candidates are matched to stored SourceTerms by their natural key
    (source, spec and placement path), so the id of a SourceTerm that is still
    wanted is kept, along with the results cached for it. Candidates have their
    derived fields and hit efficiencies filled in here: those that match
    stored SourceTerms with manually set hit efficiencies keep them. Those
    with nothing stored, or a different digest, are checked and written with
    one bulk write, and the stored ones that no candidate matches are removed.
    ValidationError is raised, before anything is written, if the results
    of a new or changed SourceTerm can't be calculated in the display units.
    """
    check_writable(version)
    keys = _content_keys()

    wanted = {}
    for sourceterm in candidates:
        sourceterm.derive()
        wanted[sourceterm.natural_key] = sourceterm

    stored = {}
    duplicates = []
    for raw in existing.as_pymongo():
        key = (raw['source']['id'], raw['spec'],
               tuple(raw.get('placement_ids', ())))
        entry = _Stored(
            raw['_id'], raw['original_id'], len(raw['version_tags']) > 1,
            _digest(raw, keys),
            None if raw.get('hiteffs_auto', True) else
            list(raw.get('hiteffs', ())))
        if key in stored:
            duplicates.append(entry.id)
        else:
            stored[key] = entry

    index = HitEffIndex(version)
    index.load({index.get_key(st) for st in wanted.values()},
               [i for entry in stored.values() for i in entry.manual or ()] +
               [i for st in wanted.values()
                for i in getattr(st, '_manual_hiteffs', ())])

    now = datetime.datetime.now()
    user = current_user_name()
    inserts = []
    updates = []
    shared = []
    for key, sourceterm in wanted.items():
        prior = stored.get(key)
        if not sourceterm.hiteffs_auto:
            hiteffs = index.get(sourceterm._manual_hiteffs)
        elif prior is not None and prior.manual is not None:
            sourceterm.hiteffs_auto = False
            hiteffs = index.get(prior.manual)
        else:
            hiteffs = index.match(sourceterm)
        sourceterm.hiteffs = hiteffs
        sourceterm.livetimes = [hit.get_livetime(sourceterm.emissionrate)
                                for hit in hiteffs]
        sourceterm.stamp = sourceterm.content_stamp()
        doc = sourceterm.to_mongo()
        if prior is not None and _digest(doc, keys) == prior.digest:
            continue
        sourceterm.original_id = (sourceterm.id if prior is None
                                  else prior.original_id)
        sourceterm.validate(clean=False)
        if prior is None:
            sourceterm.revision = 1
            sourceterm.modified = now
            if user:
                sourceterm.enteredby = user
            inserts.append(InsertOne(sourceterm.to_mongo()))
            continue
        changes = {k: v for k, v in doc.items() if k in keys}
        changes['modified'] = now
        if user:
            changes['enteredby'] = user
        update = {'$set': changes, '$inc': {'revision': 1}}
        gone = {k: '' for k in keys if k not in doc}
        if gone:
            update['$unset'] = gone
        updates.append(UpdateOne(
            {'original_id': prior.original_id, 'version_tags': version},
            update))
        if prior.shared:
            shared.append(prior.id)

    orphans = duplicates + [entry.id for key, entry in stored.items()
                            if key not in wanted]
    if shared:
        # give this version its own copies of the documents it shares
        SourceTerm.select_version(version)(id__in=shared).prepare_edit()
    if inserts or updates:
        SourceTerm._get_collection().bulk_write(inserts + updates,
                                                ordered=False)
    if orphans:
        SourceTerm.select_version(version)(id__in=orphans).delete()
    return SyncResult(len(inserts), len(updates), len(orphans))
