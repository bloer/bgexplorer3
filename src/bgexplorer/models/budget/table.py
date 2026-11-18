""" The contributions table: every displayed scalar and ROI for each placement """
from collections import defaultdict
from typing import List, Optional, Tuple
import numpy as np
from ..component import Component, Assembly
from ..settings import get_settings
from .filters import BudgetFilter
from .grouping import (COMPONENT_DEPTH, Output, filtered_cached,
                       levels, placement_names, resolve_root)
from .terms import term_table


def table_columns(version: str) -> List[Tuple[str, str]]:
    """ (key, display name) for each scalar, then each ROI, configured to be
    displayed in `version`'s contributions table. A scalar replaces an ROI
    with the same key.
    """
    config = get_settings(version).hiteffdbconfig
    columns = {key: entry.display_name or key
               for key, entry in config.display_scalars.items()
               if not entry.hide}
    hidden = {key for key, entry in config.display_scalars.items()
              if entry.hide}
    for roi in config.rois:
        if not roi.hide and roi.key not in hidden:
            columns.setdefault(roi.key, roi.key)
    return list(columns.items())


def path_sums(tt, filters: BudgetFilter, levels_of) -> Tuple[dict, int]:
    """ The numbers of the terms of `tt` passing `filters` that are summed into
    each component path below `filters.root`, and how many pass.
    `levels_of(path)` gives the lengths of the prefixes of a term's path that
    it is summed into
    """
    sums = defaultdict(list)
    count = 0
    root = filters.root
    for i, keys in enumerate(tt.keys):
        if not filters.matches(keys):
            continue
        count += 1
        path = keys['component'][len(root):]
        for level in levels_of(path):
            sums[path[:level]].append(i)
    return sums, count


def path_values(tt, outs: dict, sums: dict):
    """ A function giving, for a component path, each of the scalars and ROIs
    of `outs` (by key) summed over its terms in `sums`, as Quantities or None
    """
    having = {key: tt.having(key) for key in outs}

    def values(path):
        terms = np.array(sums.get(path, ()), dtype=np.int64)
        return {key: out.quantity(out.total(tt, key,
                                            terms[having[key][terms]]))
                for key, out in outs.items()}
    return values


@filtered_cached
def table(obj: Component, filters: Optional[BudgetFilter] = None,
          relativeto: Optional[Assembly] = None,
          depth: int = COMPONENT_DEPTH) -> dict:
    """ The contributions table of `obj` (optionally only as placed in
    `relativeto`): the sums of each displayed scalar and ROI for the SourceTerms
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
    chart_root, breadcrumb = resolve_root(obj, filters.root, relativeto)
    tt = term_table(obj, relativeto)
    version = obj.active_version
    config = get_settings(version).hiteffdbconfig
    outs = {key: Output(config.scalar_unit(key))
            for key, name in table_columns(version)}
    columns = [dict(key=key, name=name, units=outs[key].unit)
               for key, name in table_columns(version)]
    sums, count = path_sums(tt, filters,
                            lambda path: levels(path, depth, total=True))
    values = path_values(tt, outs, sums)
    result = [dict(label=breadcrumb[-1][1], depth=0, values=values(()))]
    result += [dict(label=name, depth=len(path), values=values(path))
               for path, (name, children)
               in placement_names(chart_root, depth).items()]
    return dict(columns=columns, rows=result, count=count, ntotal=len(tt))
