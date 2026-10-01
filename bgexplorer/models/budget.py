""" Break down a component's background budget by child component, isotope,
material or source category, like the summary plots in e.g. the nEXO
sensitivity papers. The breakdowns can be filtered to drill down into the
model, crossfilter style.
"""
import json
from collections import defaultdict
from typing import List, NamedTuple, Optional, Tuple
from .component import Component, Assembly
from .sourceterm import (CalculatedResults, SourceTerm, ResultsCache,
                         find_sourceterms, load_hiteffs, _cache_key, _cached,
                         _ref_id)
from .settings import get_settings
from .histogram import Histogram
from .common import units as unitreg

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


def _component_path(st: SourceTerm, child_ids: set) -> Tuple[str, ...]:
    """ The placement ids in `st`'s path below the object whose children's
    placement ids are `child_ids`. Empty for the object's own sources
    """
    pids = [str(pid) for pid in st.placement_ids or []]
    for i, pid in enumerate(pids):
        if pid in child_ids:
            return tuple(pids[i:])
    return ()


def term_values(obj: Component,
                relativeto: Optional[Assembly] = None) -> List[Term]:
    """ The scalar values of each SourceTerm of `obj` (optionally only as
    placed in `relativeto`) that has any. Kept in memory until any data in
    the version changes, and shared, so must not be modified
    """
    def calculate():
        sourceterms = list(find_sourceterms(obj, relativeto))
        hiteffs = load_hiteffs(sourceterms, spectra=False)
        child_ids = {str(p.id) for p in getattr(obj, 'children', None) or []}
        terms = []
        for st in sourceterms:
            part = CalculatedResults._calculate(st, hiteffs, spectra=False)
            values = {name: value for name, value in (part or ({},))[0].items()
                      if value is not None and not isinstance(value, Histogram)}
            if not values:
                continue
            category = st.source.category
            keys = dict(component=_component_path(st, child_ids),
                        isotope=st.source.name,
                        material=st.material or NO_MATERIAL,
                        category=category.value if category else NO_CATEGORY)
            terms.append(Term(st, values, keys))
        return terms
    key = _cache_key(obj, 'terms', _ref_id(obj),
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
    AsymmetricUncertainties
    """
    def __init__(self):
        self.value = None
        self.unfiltered = None
        self.selected = False

    @staticmethod
    def _add(total, value):
        return value if total is None else total + value

    def add(self, value):
        self.value = self._add(self.value, value)

    def include(self, value, selected: bool, passing: bool):
        """ Add `value` to the unfiltered sum, and to the sum if `passing` """
        self.unfiltered = self._add(self.unfiltered, value)
        self.selected |= selected
        if passing:
            self.add(value)


def _convert(value, unit):
    return None if value is None else value.to_reduced_units().to(unit)


def _size(value) -> float:
    """ Where a value is drawn: its upper limit, or else its central value
    """
    if value is None:
        return 0.
    m = value.m
    return float(m.get_upper_limit(0.9) if m.isupperlimit()
                 else m.nominal_value)


def _rows(groups: dict, labels: dict, unit) -> List[dict]:
    """ Rows of converted sums from `groups` keyed by group key, largest
    unfiltered size first, so filtering doesn't reorder them. Rows with
    nothing passing the filters have value None
    """
    rows = [dict(key=key, label=labels.get(key, key), depth=0,
                 selected=group.selected,
                 size=_size(_convert(group.unfiltered, unit)),
                 value=_convert(group.value, unit))
            for key, group in groups.items()]
    rows.sort(key=lambda row: row['size'], reverse=True)
    return rows


def _component_rows(terms: List[Term], scalar: str, filters: BudgetFilter,
                    chart_root: Component, depth: int, unit) -> List[dict]:
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
                                         passing)
    labels = {path: names[path][0] if path in names
              else path[-1] if path else chart_root.name
              for path in groups}
    rows = _rows(groups, labels, unit)
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


def _unit(terms: List[Term], version: str, scalar: str, unit):
    if isinstance(unit, str):
        unit = unitreg.Unit(unit) if unit else None
    unit = unit or display_unit(version, scalar)
    if unit is None and terms:
        unit = terms[0].values[scalar].to_reduced_units().u
    return unit


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
    dict(key, label, depth, selected, size, value), in order of their
    unfiltered size (a float in `units`: the upper limit of limits, else the
    central value); value is None if nothing in the row passes the filters.
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
    terms = [t for t in term_values(obj, relativeto) if scalar in t.values]
    unit = _unit(terms, obj.active_version, scalar, unit)

    root = filters.root
    below = [t for t in terms if t.keys['component'][:len(root)] == root]
    charts = {}
    for groupby in groupbys:
        if groupby == 'component':
            charts[groupby] = _component_rows(below, scalar, filters,
                                              chart_root, depth, unit)
            continue
        groups = defaultdict(_Group)
        for term in below:
            key = term.keys[groupby]
            groups[key].include(term.values[scalar],
                                filters.passes(groupby, key),
                                filters.matches(term.keys, skip=groupby))
        charts[groupby] = _rows(groups, {}, unit)

    total = _Group()
    unfiltered = _Group()
    count = 0
    for term in terms:
        unfiltered.add(term.values[scalar])
        if filters.matches(term.keys):
            total.add(term.values[scalar])
            count += 1
    return dict(scalar=scalar, units=unit, count=count, ntotal=len(terms),
                total=_convert(total.value, unit),
                unfiltered=_convert(unfiltered.value, unit),
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
    columns = [dict(key=key, name=name,
                    units=_unit([t for t in terms if key in t.values],
                                version, key, None))
               for key, name in table_columns(version)]
    root = filters.root
    sums = defaultdict(dict)
    count = 0
    for term in terms:
        if not filters.matches(term.keys):
            continue
        count += 1
        path = term.keys['component'][len(root):]
        for level in {0, min(len(path), 1), min(len(path), depth)}:
            group = sums[path[:level]]
            for name, value in term.values.items():
                group[name] = _Group._add(group.get(name), value)

    def values(path):
        group = sums.get(path, {})
        return {c['key']: _convert(group.get(c['key']), c['units'])
                for c in columns}
    rows = [dict(label=breadcrumb[-1][1], depth=0, values=values(()))]
    rows += [dict(label=name, depth=len(path), values=values(path))
             for path, (name, children)
             in _placement_names(chart_root, depth).items()]
    return dict(columns=columns, rows=rows, count=count, ntotal=len(terms))
