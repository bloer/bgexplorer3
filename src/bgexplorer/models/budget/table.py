""" The contributions table: every displayed scalar for each placement """
from collections import defaultdict
from typing import List, Optional, Tuple
from ..component import Component, Assembly
from ..settings import get_settings
from ..asymmetric import sum_values
from .filters import BudgetFilter
from .grouping import (COMPONENT_DEPTH, Output, filtered_cached,
                       levels, placement_names, resolve_root)
from .terms import term_values


def table_columns(version: str) -> List[Tuple[str, str]]:
    """ (scalar, display name) for each scalar configured to be displayed
    in `version`'s contributions table
    """
    config = get_settings(version).hiteffdbconfig.display_scalars
    return [(key, entry.display_name or key)
            for key, entry in config.items() if not entry.hide]


@filtered_cached
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
    shared, so must not be modified. Raises ValueError for an unknown root,
    and UnitMismatchError if saved data has units that don't fit together.
    """
    chart_root, breadcrumb = resolve_root(obj, filters.root)
    terms = term_values(obj, relativeto)
    version = obj.active_version
    config = get_settings(version).hiteffdbconfig
    outs = {key: Output(config.scalar_unit(key))
            for key, name in table_columns(version)}
    columns = [dict(key=key, name=name, units=outs[key].unit)
               for key, name in table_columns(version)]
    root = filters.root
    sums = defaultdict(lambda: defaultdict(list))
    count = 0
    for term in terms:
        if not filters.matches(term.keys):
            continue
        count += 1
        path = term.keys['component'][len(root):]
        for level in levels(path, depth, total=True):
            group = sums[path[:level]]
            for name, value in term.values.items():
                group[name].append(value)

    def values(path):
        group = sums.get(path, {})
        return {key: out.quantity(sum_values(group.get(key, ())))
                for key, out in outs.items()}
    result = [dict(label=breadcrumb[-1][1], depth=0, values=values(()))]
    result += [dict(label=name, depth=len(path), values=values(path))
               for path, (name, children)
               in placement_names(chart_root, depth).items()]
    return dict(columns=columns, rows=result, count=count, ntotal=len(terms))
