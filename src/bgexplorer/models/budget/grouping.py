""" What the breakdowns have in common: the component tree, groups of values,
unit output and the cache of filtered results
"""
import functools
import inspect
import json
from collections import defaultdict
from typing import List, Optional, Tuple
from ..component import Component, Assembly
from ..sourceterm import ResultsCache, _cache_key, _ref_id
from ..common import units as unitreg
from ..termcalc import conversion_factor
from .filters import GROUPBY, BudgetFilter
from .terms import available_scalars
from .termtable import TermTable

# levels of children shown in the dashboard's component breakdown
COMPONENT_DEPTH = 2
# filtered breakdowns, kept apart from the per-term values they are summed
# from, which are slower to calculate
_filtered_cache = ResultsCache(256)


def _cache_arg(arg):
    """ A hashable version of an argument of a cached breakdown """
    if isinstance(arg, (list, tuple)):
        return tuple(_cache_arg(a) for a in arg)
    if arg is None or isinstance(arg, (str, int, float)):
        return arg
    if isinstance(arg, BudgetFilter):
        return json.dumps(arg.todict(), sort_keys=True)
    if isinstance(arg, (Component, Assembly)):
        return _ref_id(arg)
    return str(arg)


def cached_filtered(obj: Component, filters: BudgetFilter,
                    relativeto: Optional[Assembly], args: tuple, calculate):
    """ The result of `calculate()` for `obj`, `filters`, `relativeto` and
    other `args`, kept in memory with the filtered breakdowns until any data
    in the version changes. Shared, so must not be modified
    """
    key = _cache_key(obj, _cache_arg(obj), _cache_arg(relativeto),
                     _cache_arg(filters), *_cache_arg(args))
    return _filtered_cache.get(key, calculate)


def filtered_cached(func):
    """ Decorator for breakdown functions with arguments `obj`, `filters` and
    `relativeto`, and others: keep their results in memory as
    `cached_filtered` does, and make `filters` a BudgetFilter if it is None
    """
    signature = inspect.signature(func)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        if arguments['filters'] is None:
            arguments['filters'] = BudgetFilter()
        others = tuple(value for name, value in arguments.items()
                       if name not in ('obj', 'filters', 'relativeto'))
        return cached_filtered(arguments['obj'], arguments['filters'],
                               arguments['relativeto'],
                               (func.__name__,) + others,
                               lambda: func(*bound.args, **bound.kwargs))
    return wrapper


def check_args(obj: Component, scalar: str, groupbys) -> None:
    """ Raise ValueError for an unknown groupby or scalar """
    for groupby in groupbys:
        if groupby not in GROUPBY:
            raise ValueError(f"groupby must be one of {', '.join(GROUPBY)}")
    if scalar not in available_scalars(obj.active_version):
        raise ValueError(f"Unknown scalar '{scalar}'")


def placement_names(component: Component, depth: int) -> dict:
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


def resolve_root(obj: Component, root: Tuple[str, ...]):
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


def levels(path: Tuple[str, ...], depth: int, total: bool = False) -> set:
    """ The lengths of the prefixes of a component `path` (relative to the
    root) that a SourceTerm is summed into: the child placement it is in, and
    the one `depth` levels down, and if `total`, the root itself
    """
    result = {min(len(path), 1), min(len(path), depth)}
    if total:
        result.add(0)
    return result


class Group:
    """ Some SourceTerms (numbered in a `TermTable`): those passing the
    filters, and all of them, to be summed by `Output.total`.

    Also the mean confidence rating of those passing, weighted by their
    sizes (see `TermTable.sizes`), and the fraction of the size that is
    unrated
    """
    def __init__(self):
        self.passing = []
        self.unfiltered = []
        self.selected = False
        self.rated_sum = 0.
        self.rated_size = 0.
        self.unrated_size = 0.

    def include(self, term: int, selected: bool, passing: bool,
                size: float = 0., rating: Optional[int] = None):
        """ Add term number `term` to the unfiltered terms, and to those
        passing and the confidence if `passing`. `size` is its size,
        `rating` its confidence rating
        """
        self.unfiltered.append(term)
        self.selected |= selected
        if passing:
            self.passing.append(term)
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


class Output:
    """ Presents the sums of a scalar, which are in its display unit
    `display`, in `unit` instead if that is given (a Unit or string).
    Raises DimensionalityError if `unit` doesn't fit
    """
    def __init__(self, display, unit=None):
        if isinstance(unit, str):
            unit = unitreg.Unit(unit) if unit else None
        self.unit = unit or display or unitreg.Unit('')
        self.factor = (conversion_factor(display, unit)
                       if unit and display else 1.)

    def total(self, table: TermTable, scalar: str, terms):
        """ The sum of `scalar` of the terms numbered `terms`, in the unit
        (an AsymmetricUncertainty, or None if there are none). Measurements
        and upper limits are summed together
        """
        return table.total(scalar, terms, self.factor)

    def quantity(self, value):
        """ `value` (from `total`, or None) as a Quantity """
        if value is None:
            return None
        return unitreg.Quantity(value, self.unit)

    @staticmethod
    def size(value) -> float:
        """ Where a value from `total` is drawn: its upper limit, or else its
        central value
        """
        if value is None:
            return 0.
        return float(value.get_upper_limit(0.9) if value.isupperlimit()
                     else value.nominal_value)


def rows(groups: dict, labels: dict, out: Output, table: TermTable,
         scalar: str) -> List[dict]:
    """ Rows of output sums of `scalar` from `groups` keyed by group key,
    largest unfiltered size first, so filtering doesn't reorder them. Rows
    with nothing passing the filters have value None
    """
    result = []
    for key, group in groups.items():
        unfiltered = out.total(table, scalar, group.unfiltered)
        result.append(dict(
            key=key, label=labels.get(key, key), depth=0,
            selected=group.selected, size=out.size(unfiltered),
            value=out.quantity(out.total(table, scalar, group.passing)),
            confidence=group.confidence))
    result.sort(key=lambda row: row['size'], reverse=True)
    return result


def component_rows(table: TermTable, terms: List[int], scalar: str,
                   filters: BudgetFilter, chart_root: Component, depth: int,
                   out: Output, sizes) -> List[dict]:
    """ Rows for each child of `chart_root` (at path `filters.root`), each
    followed by rows for its own children down to `depth` levels, from
    the numbers of the `terms` of `table` below the root, with their `sizes`
    (an array by term number). Only plain Components have sources of their own
    (Assemblies' sources are all from their children), so if `chart_root`
    is one, its single row has key None, which can't be filtered on. Rows
    also have `children`, whether they are Assemblies that can be drilled
    into
    """
    root = filters.root
    names = placement_names(chart_root, depth)
    groups = defaultdict(Group)
    for term in terms:
        keys = table.keys[term]
        path = keys['component'][len(root):]
        selected = filters.passes('component', keys['component'])
        passing = filters.matches(keys, skip='component')
        for level in levels(path, depth):
            groups[path[:level]].include(term, selected, passing,
                                         sizes[term], table.ratings[term])
    labels = {path: names[path][0] if path in names
              else path[-1] if path else chart_root.name
              for path in groups}
    result = rows(groups, labels, out, table, scalar)
    for row in result:
        row['children'] = row['key'] in names and names[row['key']][1]
    # children after their parent, and full paths from obj as keys
    ordered = []
    for row in result:
        path = row['key']
        if len(path) > 1:
            continue
        ordered.append(row)
        for child in result:
            if len(child['key']) > 1 and child['key'][:1] == path:
                child['depth'] = len(child['key']) - 1
                ordered.append(child)
    for row in ordered:
        row['key'] = root + row['key'] if row['key'] else None
    return ordered
