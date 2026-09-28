""" Break down a component's background budget by child component, isotope
or material, like the summary plots in e.g. the nEXO sensitivity papers
"""
from collections import defaultdict
from typing import List, Optional
from .component import Component, Assembly
from .sourceterm import (CalculatedResults, SourceTerm, find_sourceterms,
                         load_hiteffs)
from .settings import get_settings
from .histogram import Histogram
from .common import units as unitreg

GROUPBY = ('component', 'isotope', 'material')
NO_MATERIAL = '(no material)'


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


def _group_labeler(obj: Component, groupby: str):
    """ A function giving the group label of a SourceTerm """
    if groupby == 'isotope':
        return lambda st: st.source.name
    if groupby == 'material':
        return lambda st: st.material or NO_MATERIAL
    # component: the placement of one of obj's children in the path
    children = {p.id: p.name for p in getattr(obj, 'children', None) or []}

    def label(st):
        for pid in st.placement_ids or []:
            if pid in children:
                return children[pid]
        # sources of obj itself
        return obj.name
    return label


def budget_breakdown(obj: Component, scalar: str, groupby: str,
                     relativeto: Optional[Assembly] = None,
                     unit=None) -> dict:
    """ Sum the result `scalar` of `obj` (optionally only as placed in
    `relativeto`) in groups by 'component' (the direct children of `obj`),
    'isotope' or 'material'. Each group is split into measured sources and
    those with upper-limit emission rates.

    Returns dict(scalar, groupby, units, rows), with rows of
    (label, measured, limit) as quantities or None, largest first.
    Raises ValueError for an unknown groupby or scalar.
    """
    if groupby not in GROUPBY:
        raise ValueError(f"groupby must be one of {', '.join(GROUPBY)}")
    version = obj.active_version
    if scalar not in available_scalars(version):
        raise ValueError(f"Unknown scalar '{scalar}'")
    if isinstance(unit, str):
        unit = unitreg.Unit(unit) if unit else None
    unit = unit or display_unit(version, scalar)

    sourceterms = list(find_sourceterms(obj, relativeto))
    hiteffs = load_hiteffs(sourceterms, spectra=False)
    label = _group_labeler(obj, groupby)
    groups = defaultdict(lambda: dict(measured=None, limit=None))
    for st in sourceterms:
        part = CalculatedResults._calculate(st, hiteffs, spectra=False)
        value = part and part[0].get(scalar)
        if value is None:
            continue
        if isinstance(value, Histogram):
            continue
        group = groups[label(st)]
        kind = 'limit' if _is_limit(st) else 'measured'
        group[kind] = value if group[kind] is None else group[kind] + value

    rows = []
    for name, group in groups.items():
        for kind, value in group.items():
            if value is None:
                continue
            value = value.to_reduced_units()
            if unit is None:
                unit = value.u
            group[kind] = value.to(unit)
        rows.append(dict(label=name, **group))

    def size(row):
        return max([r.m.get_upper_limit(0.9) if kind == 'limit'
                    else r.m.nominal_value
                    for kind in ('measured', 'limit')
                    if (r := row[kind]) is not None] or [0])
    rows.sort(key=size, reverse=True)
    return dict(scalar=scalar, groupby=groupby, units=unit, rows=rows)
