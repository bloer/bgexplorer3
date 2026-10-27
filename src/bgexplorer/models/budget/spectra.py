""" Spectra summed over SourceTerms, optionally broken down by one groupby """
from collections import defaultdict
from typing import List, Optional, Tuple
import numpy as np
from ..component import Component, Assembly
from ..settings import get_settings
from ..histogram import Histogram
from ..common import units as unitreg
from ..asymmetric import sum_values
from ..sourceterm import _cache_key, _cached, _ref_id
from .filters import GROUPBY, BudgetFilter
from .grouping import filtered_cached, placement_names, resolve_root
from .terms import term_values


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
        return {t.sourceterm_id: _spectrum_size(t.values[name])
                for t in term_values(obj, relativeto, spectra=True)
                if name in t.values}
    return _cached(_cache_key(obj, 'spectrum sizes', _ref_id(obj),
                              relativeto and _ref_id(relativeto), name),
                   calculate)


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
        size = term_sizes.get(term.sourceterm_id, 0.)
        if groupby and filters.below_root(term.keys):
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
    names = placement_names(chart_root, 1) if groupby == 'component' else {}
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
    return dict(total=sum_values(totals), curves=curves, other=other,
                count=count, ntotal=len(terms))
