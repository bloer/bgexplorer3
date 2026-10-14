""" Break down a component's background budget by child component, isotope,
material or source category, like the summary plots in e.g. the nEXO
sensitivity papers. The breakdowns can be filtered to drill down into the
model, crossfilter style.
"""
import json
from collections import defaultdict
import numpy as np
from typing import List, NamedTuple, Optional, Tuple
from .component import Component, Assembly
from .sourceterm import (CalculatedResults, SourceTerm, ResultsCache,
                         find_sourceterms, load_hiteffs, _cache_key, _cached,
                         _ref_id)
from .settings import get_settings
from .histogram import Histogram
from .common import units as unitreg, DimensionalityError
from .asymmetric import sum_values
from .basecalc import ScalarCalculator, conversion_factor

GROUPBY = ('component', 'isotope', 'material', 'category')
NO_MATERIAL = '(no material)'
NO_CATEGORY = '(uncategorized)'
# levels of children shown in the dashboard's component breakdown
COMPONENT_DEPTH = 2
# filtered breakdowns, kept apart from the per-term values they are summed
# from, which are slower to calculate
_filtered_cache = ResultsCache(256)


def available_scalars(version: str) -> List[str]:
    """ Scalar and ROI result names in `version`, displayed ones first """
    from .hiteff import HitEfficiency
    config = get_settings(version).hiteffdbconfig
    names = [k for k, v in config.display_scalars.items() if not v.hide]
    names += [roi.key for roi in config.rois]
    names += sorted(HitEfficiency.select_version(version)
                    .distinct('scalars_keys'))
    hidden = {k for k, v in config.display_scalars.items() if v.hide}
    return [n for n in dict.fromkeys(names) if n not in hidden]


def display_unit(version: str, scalar: str):
    """ The configured display unit for `scalar`, if any """
    config = get_settings(version).hiteffdbconfig
    for register in (config.display_scalars, config.display_spectra):
        if scalar in register and register[scalar].display_unit is not None:
            return register[scalar].display_unit
    return None


class Term(NamedTuple):
    """ One SourceTerm's value of each scalar it has, and its key for each
    groupby. The component key is the path of placement ids (as strings)
    from the object being broken down
    """
    sourceterm: SourceTerm
    values: dict
    keys: dict
    # the confidence rating of the component the SourceTerm belongs to
    rating: Optional[int] = None


class Terms(list):
    """ The Terms of a calculation. Scalar values are plain
    AsymmetricUncertainties in SI base units (see basecalc), so `units` has
    each scalar's base unit and `default_units` a readable one to display it
    in. `mismatched` are scalars whose terms have different dimensions.
    Spectra are Histograms with their own units
    """
    units: dict = {}
    default_units: dict = {}
    mismatched: frozenset = frozenset()


def _component_path(st: SourceTerm, child_ids: set) -> Tuple[str, ...]:
    """ The placement ids in `st`'s path below the object whose children's
    placement ids are `child_ids`. Empty for the object's own sources
    """
    pids = [str(pid) for pid in st.placement_ids or []]
    for i, pid in enumerate(pids):
        if pid in child_ids:
            return tuple(pids[i:])
    return ()


def term_values(obj: Component, relativeto: Optional[Assembly] = None,
                spectra: bool = False) -> Terms:
    """ The scalar values (or if `spectra`, the spectra) of each SourceTerm
    of `obj` (optionally only as placed in `relativeto`) that has any. Scalars
    are in SI base units, see `Terms`. Kept in memory until any data in the
    version changes, and shared, so must not be modified
    """
    def calculate():
        # these are large and not needed here
        sourceterms = list(find_sourceterms(obj, relativeto).exclude(
            'assemblyPath', 'livetimes', 'assemblyPathStr'))
        hiteffs = load_hiteffs(sourceterms, spectra=spectra)
        child_ids = {str(p.id) for p in getattr(obj, 'children', None) or []}
        terms = Terms()
        calculator = None if spectra else ScalarCalculator(hiteffs)
        for st in sourceterms:
            if spectra:
                part = CalculatedResults._calculate(st, hiteffs, spectra=True)
                values = (part or ({}, {}))[1]
            else:
                values = calculator.calculate(st) or {}
            values = {name: value for name, value in values.items()
                      if value is not None
                      and isinstance(value, Histogram) == spectra}
            if not values:
                continue
            category = st.source.category
            keys = dict(component=_component_path(st, child_ids),
                        isotope=st.source.name,
                        # a component without a material is listed by name
                        material=(st.material or st.componentName
                                  or NO_MATERIAL),
                        category=category.value if category else NO_CATEGORY)
            terms.append(Term(st, values, keys, st.confidence_rating))
        if calculator:
            terms.units = calculator.units
            terms.default_units = calculator.default_units
            terms.mismatched = frozenset(calculator.mismatched)
        return terms
    key = _cache_key(obj, 'spectra' if spectra else 'terms', _ref_id(obj),
                     relativeto and _ref_id(relativeto))
    return _cached(key, calculate)


class BudgetFilter:
    """ Which SourceTerms to include in a budget breakdown.

    `entries` holds, for each groupby, a list of (key, exclude) pairs.
    A SourceTerm is rejected if it matches any excluded key of a groupby,
    else accepted if that groupby has no included keys or it matches one of
    them. Component keys are paths of placement ids from the object being
    broken down, and match all SourceTerms below that placement.
    `root` is a component path that all SourceTerms must be below; the
    component breakdown shows its children.
    """
    def __init__(self, entries: Optional[dict] = None, root=()):
        self.entries = {groupby: [(self._key(groupby, key), bool(exclude))
                                  for key, exclude in values]
                        for groupby, values in (entries or {}).items()
                        if values}
        self.root = tuple(str(pid) for pid in root)

    @staticmethod
    def _key(groupby: str, key):
        if groupby == 'component':
            return tuple(str(pid) for pid in key)
        return str(key)

    @classmethod
    def from_json(cls, text: Optional[str]) -> 'BudgetFilter':
        """ Parse `{"root": [pid, ...], "<groupby>": [{"key": ...,
        "exclude": false}, ...]}`. Raises ValueError if it isn't valid
        """
        if not text:
            return cls()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"filters are not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("filters must be an object")
        root = data.pop('root', None) or []
        if not isinstance(root, list):
            raise ValueError("filter root must be a list of placement ids")
        entries = {}
        for groupby, values in data.items():
            if groupby not in GROUPBY:
                raise ValueError(f"Unknown filter '{groupby}'")
            if not isinstance(values, list):
                raise ValueError(f"filter '{groupby}' must be a list")
            entries[groupby] = []
            for value in values:
                key = value.get('key') if isinstance(value, dict) else None
                if key is None or (groupby == 'component') != \
                        isinstance(key, list):
                    raise ValueError(f"Invalid '{groupby}' filter {value!r}")
                entries[groupby].append((key, value.get('exclude', False)))
        return cls(entries, root)

    def todict(self) -> dict:
        result = {groupby: [dict(key=list(key) if isinstance(key, tuple)
                                 else key, exclude=exclude)
                            for key, exclude in values]
                  for groupby, values in self.entries.items()}
        if self.root:
            result['root'] = list(self.root)
        return result

    @staticmethod
    def _match(groupby: str, key, test) -> bool:
        if groupby == 'component':
            return key[:len(test)] == test
        return key == test

    def passes(self, groupby: str, key) -> bool:
        """ Whether a SourceTerm with `key` passes `groupby`'s entries """
        entries = self.entries.get(groupby)
        if not entries:
            return True
        if any(exclude and self._match(groupby, key, test)
               for test, exclude in entries):
            return False
        includes = [test for test, exclude in entries if not exclude]
        return not includes or any(self._match(groupby, key, test)
                                   for test in includes)

    def matches(self, keys: dict, skip: Optional[str] = None) -> bool:
        """ Whether a SourceTerm with `keys` passes all entries except
        `skip`'s and is below `root`
        """
        if keys['component'][:len(self.root)] != self.root:
            return False
        return all(self.passes(groupby, keys[groupby])
                   for groupby in self.entries if groupby != skip)


def _placement_names(component: Component, depth: int) -> dict:
    """ Names of the placements up to `depth` levels below `component`, and
    whether each has children of its own, keyed by their path of placement
    ids (as strings)
    """
    names = {}

    def walk(comp, prefix):
        for placement in getattr(comp, 'children', None) or []:
            path = prefix + (str(placement.id),)
            children = getattr(placement.component, 'children', None)
            names[path] = (placement.name, bool(children))
            if len(path) < depth:
                walk(placement.component, path)
    walk(component, ())
    return names


def _resolve_root(obj: Component, root: Tuple[str, ...]):
    """ The component at placement path `root` below `obj`, and the
    breadcrumb of (path, name) pairs leading to it
    """
    component = obj
    crumbs = [((), obj.name)]
    for i, pid in enumerate(root):
        for placement in getattr(component, 'children', None) or []:
            if str(placement.id) == pid:
                component = placement.component
                crumbs.append((root[:i+1], placement.name))
                break
        else:
            raise ValueError(f"No placement {pid} in {component.name}")
    return component, crumbs


class _Group:
    """ The sum of some SourceTerms' values passing the filters, and of all
    of them. Measurements and upper limits are summed together, as
    AsymmetricUncertainties.

    Also the mean confidence rating of those passing, weighted by their
    sizes (see `_size`), and the fraction of the size that is unrated
    """
    def __init__(self):
        self._passing = []
        self._all = []
        self.selected = False
        self.rated_sum = 0.
        self.rated_size = 0.
        self.unrated_size = 0.

    def add(self, value):
        self._passing.append(value)

    @property
    def value(self):
        """ The sum of the values passing, or None """
        return sum_values(self._passing)

    @property
    def unfiltered(self):
        """ The sum of all values, or None """
        return sum_values(self._all)

    def include(self, value, selected: bool, passing: bool,
                size: float = 0., rating: Optional[int] = None):
        """ Add `value` to the unfiltered sum, and to the sum and the
        confidence if `passing`. `size` is the `_size` of `value` in the
        units of the rows, `rating` the term's confidence rating
        """
        self._all.append(value)
        self.selected |= selected
        if passing:
            self.add(value)
            if rating is None:
                self.unrated_size += size
            else:
                self.rated_sum += size * rating
                self.rated_size += size

    @property
    def confidence(self) -> dict:
        """ dict(mean, unrated): the size-weighted mean rating of the rated
        terms passing (None if none, or they have no size), and the fraction
        of the size of all terms passing that is unrated
        """
        total = self.rated_size + self.unrated_size
        return dict(mean=self.rated_sum / self.rated_size
                    if self.rated_size else None,
                    unrated=self.unrated_size / total if total else 0.)


class _Conversion:
    """ Converts sums of a scalar, which are in base units, to the unit to
    display them in. Raises DimensionalityError if the unit is wrong, or the
    terms' scalars have different dimensions
    """
    def __init__(self, terms: Terms, version: str, scalar: str, unit):
        if isinstance(unit, str):
            unit = unitreg.Unit(unit) if unit else None
        unit = unit or display_unit(version, scalar)
        if unit is None:
            unit = terms.default_units.get(scalar)
        self.unit = unit
        self.factor = 1.
        if scalar in terms.units:
            if scalar in terms.mismatched:
                raise DimensionalityError(
                    terms.units[scalar], terms.units[scalar],
                    extra_msg=f": results named '{scalar}' have different "
                              f"units")
            self.factor = conversion_factor(terms.units[scalar], unit)

    def quantity(self, value):
        """ `value` (an AsymmetricUncertainty or None) as a Quantity """
        if value is None:
            return None
        return unitreg.Quantity(value * self.factor, self.unit)

    def size(self, value) -> float:
        """ Where a value is drawn: its upper limit, or else its central
        value, in the unit
        """
        if value is None:
            return 0.
        return float(value.get_upper_limit(0.9) if value.isupperlimit()
                     else value.nominal_value) * self.factor


def _rows(groups: dict, labels: dict, conv: _Conversion) -> List[dict]:
    """ Rows of converted sums from `groups` keyed by group key, largest
    unfiltered size first, so filtering doesn't reorder them. Rows with
    nothing passing the filters have value None
    """
    rows = [dict(key=key, label=labels.get(key, key), depth=0,
                 selected=group.selected,
                 size=conv.size(group.unfiltered),
                 value=conv.quantity(group.value),
                 confidence=group.confidence)
            for key, group in groups.items()]
    rows.sort(key=lambda row: row['size'], reverse=True)
    return rows


def _component_rows(terms: List[Term], scalar: str, filters: BudgetFilter,
                    chart_root: Component, depth: int, conv: _Conversion,
                    sizes: dict) -> List[dict]:
    """ Rows for each child of `chart_root` (at path `filters.root`), each
    followed by rows for its own children down to `depth` levels, from
    `terms` below the root. Only plain Components have sources of their own
    (Assemblies' sources are all from their children), so if `chart_root`
    is one, its single row has key None, which can't be filtered on. Rows
    also have `children`, whether they are Assemblies that can be drilled
    into
    """
    root = filters.root
    names = _placement_names(chart_root, depth)
    groups = defaultdict(_Group)
    for term in terms:
        path = term.keys['component'][len(root):]
        selected = filters.passes('component', term.keys['component'])
        passing = filters.matches(term.keys, skip='component')
        for level in {min(len(path), 1), min(len(path), depth)}:
            groups[path[:level]].include(term.values[scalar], selected,
                                         passing, sizes[id(term)],
                                         term.rating)
    labels = {path: names[path][0] if path in names
              else path[-1] if path else chart_root.name
              for path in groups}
    rows = _rows(groups, labels, conv)
    for row in rows:
        row['children'] = row['key'] in names and names[row['key']][1]
    # children after their parent, and full paths from obj as keys
    ordered = []
    for row in rows:
        path = row['key']
        if len(path) > 1:
            continue
        ordered.append(row)
        for child in rows:
            if len(child['key']) > 1 and child['key'][:1] == path:
                child['depth'] = len(child['key']) - 1
                ordered.append(child)
    for row in ordered:
        row['key'] = root + row['key'] if row['key'] else None
    return ordered


def _check_args(obj: Component, scalar: str, groupbys):
    for groupby in groupbys:
        if groupby not in GROUPBY:
            raise ValueError(f"groupby must be one of {', '.join(GROUPBY)}")
    if scalar not in available_scalars(obj.active_version):
        raise ValueError(f"Unknown scalar '{scalar}'")


def _filtered_key(obj: Component, filters: BudgetFilter,
                  relativeto: Optional[Assembly], *args) -> Optional[tuple]:
    """ Key in the filtered results cache """
    return _cache_key(obj, _ref_id(obj), relativeto and _ref_id(relativeto),
                      json.dumps(filters.todict(), sort_keys=True), *args)


def cached_filtered(obj: Component, filters: BudgetFilter,
                    relativeto: Optional[Assembly], args: tuple, calculate):
    """ The result of `calculate()` for `obj`, `filters`, `relativeto` and
    other `args`, kept in memory with the filtered breakdowns until any data
    in the version changes. Shared, so must not be modified
    """
    return _filtered_cache.get(_filtered_key(obj, filters, relativeto, *args),
                               calculate)


def dashboard(obj: Component, scalar: str,
              filters: Optional[BudgetFilter] = None,
              relativeto: Optional[Assembly] = None, unit=None,
              groupbys=GROUPBY, depth: int = COMPONENT_DEPTH) -> dict:
    """ Break down the result `scalar` of `obj` (optionally only as placed
    in `relativeto`) by each of `groupbys`, filtered by `filters`. Each
    breakdown uses all filters except its own, so its rows show what could
    be selected; rows its own filters reject have `selected` False.

    Returns dict(scalar, units, count, ntotal, total, unfiltered,
    breadcrumb, charts): count of the SourceTerms passing all filters out of
    ntotal with a value, their total and that of all ntotal, the breadcrumb
    of (path, name) down to `filters.root`, and charts of rows for each
    groupby. Each chart has a row for every group below the root, whatever
    the other filters, so rows stay put while filtering. Each row is
    dict(key, label, depth, selected, size, value, confidence), in order of
    their unfiltered size (a float in `units`: the upper limit of limits,
    else the central value); value is None if nothing in the row passes the
    filters. confidence is dict(mean, unrated) over the terms in value: the
    mean of their components' confidence ratings weighted by their sizes
    (None if none are rated), and the fraction of the size that is unrated.
    Measurements and upper limits are summed together, so a value is only a
    limit if everything in it is. The component rows are described in
    `_component_rows`.
    Raises ValueError for an unknown groupby, scalar or root.
    Results are kept in memory until any data in the version changes, and
    shared, so must not be modified.
    """
    filters = filters or BudgetFilter()
    key = _filtered_key(obj, filters, relativeto, 'dashboard', scalar,
                        unit and str(unit), tuple(groupbys), depth)
    return _filtered_cache.get(key, lambda: _dashboard(
        obj, scalar, filters, relativeto, unit, groupbys, depth))


def _dashboard(obj, scalar, filters, relativeto, unit, groupbys, depth):
    _check_args(obj, scalar, groupbys)
    chart_root, breadcrumb = _resolve_root(obj, filters.root)
    all_terms = term_values(obj, relativeto)
    terms = [t for t in all_terms if scalar in t.values]
    conv = _Conversion(all_terms, obj.active_version, scalar, unit)
    unit = conv.unit

    root = filters.root
    below = [t for t in terms if t.keys['component'][:len(root)] == root]
    # where each term is drawn, which is also its weight in the confidence
    sizes = {id(t): conv.size(t.values[scalar]) for t in below}
    charts = {}
    for groupby in groupbys:
        if groupby == 'component':
            charts[groupby] = _component_rows(below, scalar, filters,
                                              chart_root, depth, conv, sizes)
            continue
        groups = defaultdict(_Group)
        for term in below:
            key = term.keys[groupby]
            groups[key].include(term.values[scalar],
                                filters.passes(groupby, key),
                                filters.matches(term.keys, skip=groupby),
                                sizes[id(term)], term.rating)
        charts[groupby] = _rows(groups, {}, conv)

    total = _Group()
    unfiltered = _Group()
    count = 0
    for term in terms:
        unfiltered.add(term.values[scalar])
        if filters.matches(term.keys):
            total.add(term.values[scalar])
            count += 1
    return dict(scalar=scalar, units=unit, count=count, ntotal=len(terms),
                total=conv.quantity(total.value),
                unfiltered=conv.quantity(unfiltered.value),
                breadcrumb=breadcrumb, charts=charts)


def table_columns(version: str) -> List[Tuple[str, str]]:
    """ (scalar, display name) for each scalar configured to be displayed
    in `version`'s contributions table
    """
    config = get_settings(version).hiteffdbconfig.display_scalars
    return [(key, entry.display_name or key)
            for key, entry in config.items() if not entry.hide]


def table(obj: Component, filters: Optional[BudgetFilter] = None,
          relativeto: Optional[Assembly] = None,
          depth: int = COMPONENT_DEPTH) -> dict:
    """ The contributions table of `obj` (optionally only as placed in
    `relativeto`): the sums of each displayed scalar for the SourceTerms
    passing all of `filters`, for the component at `filters.root` and its
    placements down to `depth` levels below it.

    Returns dict(columns, rows, count, ntotal): columns of dict(key, name,
    units), and rows of dict(label, depth, values), where values maps each
    column's key to its sum in its units, or None. The first row is the
    root's total, followed by its placements in order, each followed by its
    own. count of ntotal SourceTerms with values pass the filters.
    Results are kept in memory until any data in the version changes, and
    shared, so must not be modified. Raises ValueError for an unknown root.
    """
    filters = filters or BudgetFilter()
    key = _filtered_key(obj, filters, relativeto, 'table', depth)
    return _filtered_cache.get(key, lambda: _table(obj, filters, relativeto,
                                                   depth))


def _table(obj, filters, relativeto, depth):
    chart_root, breadcrumb = _resolve_root(obj, filters.root)
    terms = term_values(obj, relativeto)
    version = obj.active_version
    convs = {key: _Conversion(terms, version, key, None)
             for key, name in table_columns(version)}
    columns = [dict(key=key, name=name, units=convs[key].unit)
               for key, name in table_columns(version)]
    root = filters.root
    sums = defaultdict(lambda: defaultdict(list))
    count = 0
    for term in terms:
        if not filters.matches(term.keys):
            continue
        count += 1
        path = term.keys['component'][len(root):]
        for level in {0, min(len(path), 1), min(len(path), depth)}:
            group = sums[path[:level]]
            for name, value in term.values.items():
                group[name].append(value)

    def values(path):
        group = sums.get(path, {})
        return {key: conv.quantity(sum_values(group.get(key, ())))
                for key, conv in convs.items()}
    rows = [dict(label=breadcrumb[-1][1], depth=0, values=values(()))]
    rows += [dict(label=name, depth=len(path), values=values(path))
             for path, (name, children)
             in _placement_names(chart_root, depth).items()]
    return dict(columns=columns, rows=rows, count=count, ntotal=len(terms))


def spectrum_names(version: str) -> List[Tuple[str, str]]:
    """ (spectrum, display name) for each spectrum in `version` that isn't
    hidden, configured ones first
    """
    from .hiteff import HitEfficiency
    config = get_settings(version).hiteffdbconfig.display_spectra
    names = list(config) + sorted(HitEfficiency.select_version(version)
                                  .distinct('spectra_keys'))
    return [(name, (config[name].display_name if name in config else None)
             or name)
            for name in dict.fromkeys(names)
            if not (name in config and config[name].hide)]


def _spectrum_size(hist: Histogram) -> float:
    """ The integral of `hist`'s approximate mean, treating each bin as a
    split normal, to rank spectra by. Unlike the most likely value, it adds
    up, and is positive for upper limits
    """
    widths = np.diff(np.asarray(getattr(hist.bin_edges, 'm', hist.bin_edges),
                                dtype=float))
    m = hist.hist.m
    if hasattr(m, 'mode'):
        mean = (np.asarray(m.mode, dtype=float) + np.sqrt(2 / np.pi)
                * (np.asarray(m.s1, dtype=float) - np.asarray(m.s0, dtype=float)))
    else:
        mean = np.asarray(m, dtype=float)
    reduced = unitreg.Quantity(1., hist.hist.u).to_reduced_units()
    return float(np.nansum(mean * widths)) * reduced.m


def _spectrum_sizes(obj, relativeto, name: str) -> dict:
    """ `_spectrum_size` of each SourceTerm's spectrum `name`, by its id.
    Cached like term_values
    """
    def calculate():
        return {t.sourceterm.id: _spectrum_size(t.values[name])
                for t in term_values(obj, relativeto, spectra=True)
                if name in t.values}
    return _cached(_cache_key(obj, 'spectrum sizes', _ref_id(obj),
                              relativeto and _ref_id(relativeto), name),
                   calculate)


def spectrum_breakdown(obj: Component, name: str,
                       filters: Optional[BudgetFilter] = None,
                       relativeto: Optional[Assembly] = None,
                       groupby: Optional[str] = None, top: int = 4) -> dict:
    """ The spectrum `name` of `obj` (optionally only as placed in
    `relativeto`), summed over the SourceTerms passing all of `filters`, and
    if `groupby`, broken down by it. Components are grouped by the
    placements below `filters.root`.

    Returns dict(total, curves, other, count, ntotal): the total Histogram
    (None if nothing passes), curves of the `top` largest groups by
    approximate mean (see `_spectrum_size`) as dict(key, label, rank,
    value), and other, the sum of the
    rest (None if there are none). A group's rank is its place among all
    of them before filtering, so it can keep its color while filtering.
    count of ntotal SourceTerms with the spectrum pass the filters.
    Results are kept in memory until any data in the version changes, and
    shared, so must not be modified. Raises ValueError for an unknown
    groupby or root.
    """
    if groupby is not None and groupby not in GROUPBY:
        raise ValueError(f"groupby must be one of {', '.join(GROUPBY)}")
    filters = filters or BudgetFilter()
    key = _filtered_key(obj, filters, relativeto, 'spectrum', name, groupby,
                        top)
    return _filtered_cache.get(key, lambda: _spectrum_breakdown(
        obj, name, filters, relativeto, groupby, top))


def _spectrum_breakdown(obj, name, filters, relativeto, groupby, top):
    chart_root, breadcrumb = _resolve_root(obj, filters.root)
    root = filters.root
    terms = [t for t in term_values(obj, relativeto, spectra=True)
             if name in t.values]

    def group_key(term):
        if groupby == 'component':
            return term.keys['component'][len(root):][:1]
        return term.keys[groupby]
    term_sizes = _spectrum_sizes(obj, relativeto, name) if groupby else {}
    totals = []
    groups = defaultdict(list)
    sizes = defaultdict(float)
    filtered_sizes = defaultdict(float)
    count = 0
    for term in terms:
        below = term.keys['component'][:len(root)] == root
        size = term_sizes.get(term.sourceterm.id, 0.)
        if groupby and below:
            sizes[group_key(term)] += size
        if not filters.matches(term.keys):
            continue
        count += 1
        totals.append(term.values[name])
        if groupby:
            groups[group_key(term)].append(term.values[name])
            filtered_sizes[group_key(term)] += size

    rank = {key: i for i, key in enumerate(
        sorted(sizes, key=sizes.get, reverse=True))}
    names = _placement_names(chart_root, 1) if groupby == 'component' else {}
    ordered = sorted(groups, key=filtered_sizes.get, reverse=True)
    # an "other" of a single group would just be that group
    shown = ordered if len(ordered) <= top + 1 else ordered[:top]
    curves = [dict(key=key, rank=rank[key],
                   value=sum_values(groups[key]),
                   label=(names[key][0] if key in names
                          else chart_root.name if key == () else key))
              for key in shown]
    other = sum_values(v for key in ordered[len(shown):]
                       for v in groups[key])
    return dict(total=sum_values(totals), curves=curves, other=other, count=count,
                ntotal=len(terms))
