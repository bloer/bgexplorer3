""" Spectra summed over SourceTerms, optionally broken down by one groupby """
from collections import defaultdict
from typing import List, Optional, Tuple
import numpy as np
from ..component import Component, Assembly
from ..settings import get_settings
from .filters import GROUPBY, BudgetFilter
from .grouping import filtered_cached, placement_names, resolve_root
from .terms import term_table


def spectrum_names(version: str) -> List[Tuple[str, str]]:
    """ (spectrum, display name) for each spectrum in `version` that isn't
    hidden, configured ones first
    """
    from ..hiteff import HitEfficiency
    config = get_settings(version).hiteffdbconfig.display_spectra
    names = list(config) + sorted(HitEfficiency.select_version(version)
                                  .distinct('spectra_keys'))
    return [(name, (config[name].display_name if name in config else None)
             or name)
            for name in dict.fromkeys(names)
            if not (name in config and config[name].hide)]


@filtered_cached
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
    The histograms are in the spectrum's display unit.
    Results are kept in memory until any data in the version changes, and
    shared, so must not be modified. Raises ValueError for an unknown
    groupby or root, and UnitMismatchError if saved data has units that
    don't fit together.
    """
    if groupby is not None and groupby not in GROUPBY:
        raise ValueError(f"groupby must be one of {', '.join(GROUPBY)}")
    chart_root, breadcrumb = resolve_root(obj, filters.root)
    root = filters.root
    tt = term_table(obj, relativeto, spectra=True)
    terms = np.flatnonzero(tt.having(name))

    def group_key(keys):
        if groupby == 'component':
            return keys['component'][len(root):][:1]
        return keys[groupby]
    term_sizes = tt.sizes(name) if groupby else None
    passing = []
    groups = defaultdict(list)
    sizes = defaultdict(float)
    filtered_sizes = defaultdict(float)
    for i in terms:
        keys = tt.keys[i]
        if groupby:
            key = group_key(keys)
            if filters.below_root(keys):
                sizes[key] += term_sizes[i]
        if not filters.matches(keys):
            continue
        passing.append(i)
        if groupby:
            groups[key].append(i)
            filtered_sizes[key] += term_sizes[i]

    rank = {key: i for i, key in enumerate(
        sorted(sizes, key=sizes.get, reverse=True))}
    names = placement_names(chart_root, 1) if groupby == 'component' else {}
    ordered = sorted(groups, key=filtered_sizes.get, reverse=True)
    # an "other" of a single group would just be that group
    shown = ordered if len(ordered) <= top + 1 else ordered[:top]
    curves = [dict(key=key, rank=rank[key],
                   value=tt.total_spectrum(name, groups[key]),
                   label=(names[key][0] if key in names
                          else chart_root.name if key == () else key))
              for key in shown]
    rest = [i for key in ordered[len(shown):] for i in groups[key]]
    return dict(total=tt.total_spectrum(name, passing), curves=curves,
                other=tt.total_spectrum(name, rest) if rest else None,
                count=len(passing), ntotal=len(terms))
