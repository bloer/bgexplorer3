""" Break down a component's background budget by child component, isotope,
material or source category, like the summary plots in e.g. the nEXO
sensitivity papers. The breakdowns can be filtered to drill down into the
model, crossfilter style.
"""
import json
from collections import defaultdict
from typing import List, NamedTuple, Optional, Tuple
from .component import Component, Assembly
from .sourceterm import (CalculatedResults, SourceTerm, find_sourceterms,
                         load_hiteffs, _cache_key, _cached, _ref_id)
from .settings import get_settings
from .histogram import Histogram
from .common import units as unitreg

GROUPBY = ('component', 'isotope', 'material', 'category')
NO_MATERIAL = '(no material)'
NO_CATEGORY = '(uncategorized)'
# levels of children shown in the dashboard's component breakdown
COMPONENT_DEPTH = 2


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


def _is_limit(sourceterm: SourceTerm) -> bool:
    rate = sourceterm.source.rate
    try:
        return bool(rate.m.isupperlimit())
    except AttributeError:
        return False


class Term(NamedTuple):
    """ One SourceTerm's value of a scalar, and its key for each groupby.
    The component key is the path of placement ids (as strings) from the
    object being broken down
    """
    sourceterm: SourceTerm
    value: object
    is_limit: bool
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


def term_values(obj: Component, scalar: str,
                relativeto: Optional[Assembly] = None) -> List[Term]:
    """ The value of `scalar` for each SourceTerm of `obj` (optionally only
    as placed in `relativeto`) that has one. Kept in memory until any data
    in the version changes, and shared, so must not be modified
    """
    def calculate():
        sourceterms = list(find_sourceterms(obj, relativeto))
        hiteffs = load_hiteffs(sourceterms, spectra=False)
        child_ids = {str(p.id) for p in getattr(obj, 'children', None) or []}
        terms = []
        for st in sourceterms:
            part = CalculatedResults._calculate(st, hiteffs, spectra=False)
            value = part and part[0].get(scalar)
            if value is None or isinstance(value, Histogram):
                continue
            category = st.source.category
            keys = dict(component=_component_path(st, child_ids),
                        isotope=st.source.name,
                        material=st.material or NO_MATERIAL,
                        category=category.value if category else NO_CATEGORY)
            terms.append(Term(st, value, _is_limit(st), keys))
        return terms
    key = _cache_key(obj, 'terms', _ref_id(obj),
                     relativeto and _ref_id(relativeto), scalar)
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
    """ Names of the placements up to `depth` levels below `component`,
    keyed by their path of placement ids (as strings)
    """
    names = {}

    def walk(comp, prefix):
        for placement in getattr(comp, 'children', None) or []:
            path = prefix + (str(placement.id),)
            names[path] = placement.name
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
    """ Sums of the measured and upper limit parts of some SourceTerms """
    def __init__(self):
        self.measured = None
        self.limit = None
        self.selected = False

    def add(self, term: Term):
        if term.is_limit:
            self.limit = term.value if self.limit is None \
                else self.limit + term.value
        else:
            self.measured = term.value if self.measured is None \
                else self.measured + term.value

    @property
    def all(self):
        if self.measured is None or self.limit is None:
            return self.measured if self.limit is None else self.limit
        return self.measured + self.limit

    def converted(self, unit, parts=('measured', 'limit')) -> dict:
        result = {}
        for kind in parts:
            value = getattr(self, kind)
            result[kind] = (None if value is None
                            else value.to_reduced_units().to(unit))
        return result


def _size(row: dict) -> float:
    """ The length of a row's bar """
    return max([r.m.get_upper_limit(0.9) if kind == 'limit'
                else r.m.nominal_value
                for kind in ('measured', 'limit')
                if (r := row[kind]) is not None] or [0])


def _rows(groups: dict, labels: dict, unit) -> List[dict]:
    """ Rows of converted sums from `groups` keyed by group key, largest
    first
    """
    rows = [dict(key=key, label=labels.get(key, key), depth=0,
                 selected=group.selected, **group.converted(unit))
            for key, group in groups.items()]
    rows.sort(key=_size, reverse=True)
    return rows


def _component_rows(terms: List[Term], filters: BudgetFilter,
                    chart_root: Component, depth: int, unit) -> List[dict]:
    """ Rows for each child of `chart_root` (at path `filters.root`), each
    followed by rows for its own children down to `depth` levels. Only
    plain Components have sources of their own (Assemblies' sources are all
    from their children), so if `chart_root` is one, its single row has key
    None, which can't be filtered on
    """
    root = filters.root
    names = _placement_names(chart_root, depth)
    groups = defaultdict(_Group)
    for term in terms:
        path = term.keys['component'][len(root):]
        selected = filters.passes('component', term.keys['component'])
        for level in {min(len(path), 1), min(len(path), depth)}:
            group = groups[path[:level]]
            group.add(term)
            group.selected |= selected
    labels = {path: names.get(path, path[-1] if path else chart_root.name)
              for path in groups}
    rows = _rows(groups, labels, unit)
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
        unit = terms[0].value.to_reduced_units().u
    return unit


def _check_args(obj: Component, scalar: str, groupbys):
    for groupby in groupbys:
        if groupby not in GROUPBY:
            raise ValueError(f"groupby must be one of {', '.join(GROUPBY)}")
    if scalar not in available_scalars(obj.active_version):
        raise ValueError(f"Unknown scalar '{scalar}'")


def dashboard(obj: Component, scalar: str,
              filters: Optional[BudgetFilter] = None,
              relativeto: Optional[Assembly] = None, unit=None,
              groupbys=GROUPBY, depth: int = COMPONENT_DEPTH) -> dict:
    """ Break down the result `scalar` of `obj` (optionally only as placed
    in `relativeto`) by each of `groupbys`, filtered by `filters`. Each
    breakdown uses all filters except its own, so its rows show what could
    be selected; rows its own filters reject have `selected` False.

    Returns dict(scalar, units, count, ntotal, total, breadcrumb, charts):
    count of the SourceTerms passing all filters out of ntotal with a value,
    their total as dict(measured, limit, all), the breadcrumb of (path, name)
    down to `filters.root`, and charts of rows for each groupby. Each row is
    dict(key, label, depth, selected, measured, limit), largest first. The
    component rows are described in `_component_rows`.
    Raises ValueError for an unknown groupby, scalar or root.
    """
    _check_args(obj, scalar, groupbys)
    filters = filters or BudgetFilter()
    chart_root, breadcrumb = _resolve_root(obj, filters.root)
    terms = term_values(obj, scalar, relativeto)
    unit = _unit(terms, obj.active_version, scalar, unit)

    charts = {}
    for groupby in groupbys:
        passing = [t for t in terms if filters.matches(t.keys, skip=groupby)]
        if groupby == 'component':
            charts[groupby] = _component_rows(passing, filters, chart_root,
                                              depth, unit)
            continue
        groups = defaultdict(_Group)
        for term in passing:
            group = groups[term.keys[groupby]]
            group.add(term)
            group.selected |= filters.passes(groupby, term.keys[groupby])
        charts[groupby] = _rows(groups, {}, unit)

    total = _Group()
    count = 0
    for term in terms:
        if filters.matches(term.keys):
            total.add(term)
            count += 1
    return dict(scalar=scalar, units=unit, count=count, ntotal=len(terms),
                total=total.converted(unit, ('measured', 'limit', 'all')),
                breadcrumb=breadcrumb, charts=charts)


def budget_breakdown(obj: Component, scalar: str, groupby: str,
                     relativeto: Optional[Assembly] = None,
                     unit=None) -> dict:
    """ Sum the result `scalar` of `obj` (optionally only as placed in
    `relativeto`) in groups by 'component' (the direct children of `obj`),
    'isotope', 'material' or 'category'. Each group is split into measured
    sources and those with upper-limit emission rates.

    Returns dict(scalar, groupby, units, rows), with rows of
    (label, measured, limit) as quantities or None, largest first.
    Raises ValueError for an unknown groupby or scalar.
    """
    _check_args(obj, scalar, [groupby])
    result = dashboard(obj, scalar, relativeto=relativeto, unit=unit,
                       groupbys=[groupby], depth=1)
    rows = [dict(label=row['label'], measured=row['measured'],
                 limit=row['limit']) for row in result['charts'][groupby]]
    return dict(scalar=scalar, groupby=groupby, units=result['units'],
                rows=rows)
