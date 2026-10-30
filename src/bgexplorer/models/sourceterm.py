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
from .verdoc import (VersionedDocument, VersionedReferenceField,
                     VersionedQuerySet, VersionedListField,
                     VersionedEmbeddedDocumentListField)
from .verdoc import ref_id as _ref_id
from .verdoc import (IDENTITY_FIELDS, check_writable, current_user_name)
from .component import (Component, Placement, Assembly, ComponentSpec,
                        LocationMode)
from .emissionspec import EmissionSpec, EmissionSource, Multiplier
from .fields import (QuantityField, UncertainQuantityField, HistogramField,
                     RawDictField)
from .asymmetric import AsymmetricUncertainty, LinearExpression
from .histogram import Histogram
from .hiteff import HitEfficiency
from .isotope import concentration_to_rate
from .common import units, addnone, multnone, Quantity, UnitMismatchError
from .termcalc import TermCalculator
from .asymmetric import sum_values
from . import settings
from typing import (List, Optional, Union, Dict, Tuple, Iterable,
                    NamedTuple, Sequence, Set)
from collections import defaultdict, OrderedDict
from bson import ObjectId
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import threading
log = logging.getLogger(__name__)
_unit_checks_deferred = ContextVar('unit_checks_deferred', default=False)
NO_MATERIAL = '(no material)'
NO_CATEGORY = '(uncategorized)'


@contextmanager
def unit_checks_deferred():
    """ Don't raise from `SourceTerm.check_units` inside this context, for
    bulk operations such as an import or merge that audit the whole version
    afterwards, see `check_version_units`
    """
    token = _unit_checks_deferred.set(True)
    try:
        yield
    finally:
        _unit_checks_deferred.reset(token)


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

    @property
    def unweighted_rate(self):
        """ The source's rate, in specific activity if it was a concentration,
        before the weight and rate multiplier are applied. None if it has none
        """
        rate = self.source.rate
        # convert ppb-like units to specific activity
        if (rate is not None and self.source.multiplier is Multiplier.mass
                and rate.dimensionless):
            rate = concentration_to_rate(self.source.name, rate)
        return rate

    @cached_property
    def emissionrate(self):
        rate = self.unweighted_rate
        if rate is None:
            return None
        return rate * self.weight * self.rate_multiplier


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

    def check_units(self, hiteffs: Optional[Iterable[HitEfficiency]] = None,
                    calculator: Optional[TermCalculator] = None) -> None:
        """ Raise ValidationError if our results can't be calculated in the
        display units of the settings, e.g. because our emission rate doesn't
        fit the normalization of one of our hit efficiencies. Check with
        `hiteffs` rather than our own if given. Results with no display unit
        yet are not checked. A `calculator` (not strict) can be shared by
        many SourceTerms to reuse what it has worked out for each hit
        efficiency.
        """
        if _unit_checks_deferred.get():
            return
        if hiteffs is None:
            hiteffs = list(self.hiteffs)
        if calculator is None:
            config = settings.get_settings(self.active_version).hiteffdbconfig
            calculator = TermCalculator(config, strict=False)
        try:
            calculator.check(self, hiteffs)
        except UnitMismatchError as e:
            raise ValidationError(f"{self.assemblyPathStr}: {e}") from e

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
        self.clear_results()
        self.derive()
        self.livetimes = [hit.get_livetime(self.emissionrate)
                          for hit in self.hiteffs]
        self.check_units()
        self.stamp = self.content_stamp()

    @property
    def natural_key(self) -> tuple:
        """ What identifies this SourceTerm within its assembly root and
        version. Needs `derive` to have set the placement ids
        """
        return (self.source.id, _ref_id(self.spec), tuple(self.placement_ids))

    def clear_results(self):
        """ Remove all CalculatedResults in the database referencing
        this object
        """
        CalculatedResults.objects(sources=self).delete()


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
    """ Normalized HitEff results stored in the database.

    Legacy: nothing saves these any more. The budget pages sum the
    SourceTerms in memory (`budget.term_values`) behind the in-memory
    `ResultsCache`s, and `for_object` only stores results if called with
    `save=True`. The machinery is kept because the stored expressions
    (`scalars_expr`, `spectra_expr`) allow correlations to be reconstructed
    from stored results, and is still invalidated when SourceTerms or
    HitEfficiencies change.
    """
    # TODO: should this be a versioned document? it's acting as a cache
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
                   save: bool = False, spectra: bool = True,
                   cache: bool = True, correlations: bool = True):
        """ Calculate results for all SourceTerms of `obj`, optionally
        only the part in assembly `relativeto`. If `cache`, results are
        kept in memory until any data in the version changes. If `save`,
        they are also stored in the database (see `CalculatedResults`).

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
            for key, val in hiteff.all_scalars.items():
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
        scalar_parts, hist_parts = defaultdict(list), defaultdict(list)
        found = False
        for part in parts:
            if part is None:
                continue
            found = True
            for key, val in part[0].items():
                scalar_parts[key].append(val)
            for key, val in part[1].items():
                hist_parts[key].append(val)
        if not found:
            return None
        scalars = {key: sum_values(vals) for key, vals in scalar_parts.items()}
        hists = {key: sum_values(vals) for key, vals in hist_parts.items()}
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


class _Flight:
    """ A calculation in progress, which other threads can wait for """
    def __init__(self):
        self.done = threading.Event()
        self.thread = threading.get_ident()


class ResultsCache:
    """ In-memory LRU cache of calculated results. Keys start with the
    version's cache token, which changes whenever any data in the version
    changes, so entries never need invalidating and it is safe with multiple
    processes. Results are shared, so must not be modified.

    If several threads want the same missing key at once, one calculates it
    and the others wait for it, rather than all repeating a slow calculation.
    """
    instances = []

    def __init__(self, size: int):
        self.size = size
        self._entries = OrderedDict()
        self._flights = {}
        self._lock = threading.Lock()
        ResultsCache.instances.append(self)

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
                    self._entries.move_to_end(key)
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
                self._entries[key] = result
                while len(self._entries) > self.size:
                    self._entries.popitem(last=False)
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
                    self._entries.move_to_end(key)
                    found[key] = result
        return found

    def put_many(self, items: Dict) -> None:
        """ Cache several results at once """
        with self._lock:
            self._entries.update(items)
            for key in items:
                self._entries.move_to_end(key)
            while len(self._entries) > self.size:
                self._entries.popitem(last=False)

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


def hiteff_rate_units(hiteff: HitEfficiency) -> set:
    """ The units of the emission rates of the SourceTerms in the version that
    `hiteff` applies to, matched by source, location and material
    """
    query = SourceTerm.select_version(hiteff.active_version)(
        source__name=hiteff.source, location=hiteff.location)
    if hiteff.material:
        query = query(material=hiteff.material)
    found = (TermCalculator.rate_unit(st)
             for st in query.only('version_tags', 'source', 'weight',
                                 'rate_multiplier'))
    return {unit for unit in found if unit is not None}


def check_version_units(version: str, batch_size: int = 500) -> List[str]:
    """ Audit the units of everything in `version`, for data that was written
    without being validated, such as an import or a merge. Scalars, spectra
    and ROIs of the HitEfficiencies without a display unit in the settings
    are given one, then every SourceTerm is checked. Returns a description
    of each problem found, which is empty if the units all agree.
    """
    vsettings = settings.get_settings(version, create=False)
    config = vsettings.hiteffdbconfig
    before = config.to_mongo()
    for hiteff in HitEfficiency.select_version(version):
        config.update_from(hiteff)
    if config.to_mongo() != before:
        # update rather than save: the version may be locked or read-only
        vsettings.update(set__hiteffdbconfig=config)
        settings.touch(version)

    problems = []
    seen = set()
    query = SourceTerm.select_version(version).order_by('id')
    start = 0
    while batch := list(query.skip(start).limit(batch_size)):
        start += batch_size
        calculator = TermCalculator(config, load_hiteffs(batch, spectra=False))
        for sourceterm in batch:
            try:
                calculator.check(sourceterm)
            except UnitMismatchError as e:
                if str(e) not in seen:
                    seen.add(str(e))
                    problems.append(f"{sourceterm.assemblyPathStr}: {e}")
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
        self._by_key = defaultdict(list)
        self._by_id = {}
        self._sources = set()

    def load(self, sources: Iterable[str], ids: Iterable[ObjectId] = ()
             ) -> None:
        """ Load every HitEfficiency of the named `sources` for `match`, and
        those with the given original ids for `get`, if not already loaded
        """
        query = HitEfficiency.select_version(self.version)
        new = set(sources) - self._sources
        if new:
            self._sources |= new
            for hiteff in query(source__in=sorted(new)).exclude('spectra'):
                self._by_id[hiteff.original_id] = hiteff
                self._by_key[hiteff.source, hiteff.location].append(hiteff)
        missing = set(ids) - self._by_id.keys()
        if missing:
            for hiteff in query(original_id__in=list(missing)
                                ).exclude('spectra'):
                self._by_id[hiteff.original_id] = hiteff

    def match(self, sourceterm: SourceTerm) -> List[HitEfficiency]:
        """ The HitEfficiencies that apply to `sourceterm` (those of its source
        and location, with no material or its material), whose location and
        material must already be set
        """
        return [hiteff for hiteff in self._by_key.get(
                    (sourceterm.source.name, sourceterm.location), ())
                if hiteff.material in (None, sourceterm.material)]

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
    index.load({st.source.name for st in wanted.values()},
               [i for entry in stored.values() for i in entry.manual or ()] +
               [i for st in wanted.values()
                for i in getattr(st, '_manual_hiteffs', ())])
    calculator = TermCalculator(
        settings.get_settings(version).hiteffdbconfig, strict=False)

    now = datetime.datetime.now()
    user = current_user_name()
    inserts = []
    updates = []
    shared = []
    unshared = []
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
        sourceterm.check_units(hiteffs, calculator)
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
        (shared if prior.shared else unshared).append(prior.id)

    orphans = duplicates + [entry.id for key, entry in stored.items()
                            if key not in wanted]
    if shared:
        # give this version its own copies of the documents it shares
        SourceTerm.select_version(version)(id__in=shared).prepare_edit()
    if unshared:
        CalculatedResults.objects(sources__in=unshared).delete()
    if inserts or updates:
        SourceTerm._get_collection().bulk_write(inserts + updates,
                                                ordered=False)
    if orphans:
        SourceTerm.select_version(version)(id__in=orphans).delete()
    return SyncResult(len(inserts), len(updates), len(orphans))
