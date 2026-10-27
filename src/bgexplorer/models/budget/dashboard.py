""" The dashboard: a scalar broken down by every groupby at once """
from collections import defaultdict
from typing import Optional
from ..component import Component, Assembly
from ..settings import get_settings
from .filters import GROUPBY, BudgetFilter
from .grouping import (COMPONENT_DEPTH, Group, Output, check_args,
                       component_rows, filtered_cached, resolve_root, rows)
from .terms import term_values


@filtered_cached
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
    `component_rows`. The values are in `unit` or else the scalar's display
    unit in the version's settings.
    Raises ValueError for an unknown groupby, scalar or root, and
    UnitMismatchError if saved data has units that don't fit together.
    Results are kept in memory until any data in the version changes, and
    shared, so must not be modified.
    """
    check_args(obj, scalar, groupbys)
    chart_root, breadcrumb = resolve_root(obj, filters.root)
    terms = [t for t in term_values(obj, relativeto) if scalar in t.values]
    out = Output(get_settings(obj.active_version).hiteffdbconfig
                 .scalar_unit(scalar), unit)

    below = [t for t in terms if filters.below_root(t.keys)]
    # where each term is drawn, which is also its weight in the confidence
    sizes = {id(t): out.size(t.values[scalar]) for t in below}
    charts = {}
    for groupby in groupbys:
        if groupby == 'component':
            charts[groupby] = component_rows(below, scalar, filters,
                                             chart_root, depth, out, sizes)
            continue
        groups = defaultdict(Group)
        for term in below:
            key = term.keys[groupby]
            groups[key].include(term.values[scalar],
                                filters.passes(groupby, key),
                                filters.matches(term.keys, skip=groupby),
                                sizes[id(term)], term.rating)
        charts[groupby] = rows(groups, {}, out)

    total = Group()
    unfiltered = Group()
    count = 0
    for term in terms:
        unfiltered.add(term.values[scalar])
        if filters.matches(term.keys):
            total.add(term.values[scalar])
            count += 1
    return dict(scalar=scalar, units=out.unit, count=count, ntotal=len(terms),
                total=out.quantity(total.value),
                unfiltered=out.quantity(unfiltered.value),
                breadcrumb=breadcrumb, charts=charts)
