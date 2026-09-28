""" Convert results to plain JSON for the plotly charts in bgplots.js """
from typing import Optional
import numpy as np
from ..models.common import units as unitreg
from ..models.histogram import Histogram


def unit_str(unit) -> str:
    """ Short printable form of a unit """
    return '' if unit is None else f"{unitreg.Unit(unit):~P}"


def _parts(m):
    """ (mode, lower error, upper error) arrays of an AsymmetricUncertainty
    or plain number(s)
    """
    if hasattr(m, 'mode'):
        return (np.asarray(m.mode, dtype=float),
                np.asarray(m.s0, dtype=float), np.asarray(m.s1, dtype=float))
    value = np.asarray(m, dtype=float)
    return value, np.zeros_like(value), np.zeros_like(value)


def _tolist(a) -> list:
    """ JSON-safe list: non-finite values become None """
    return [float(x) if np.isfinite(x) else None for x in np.ravel(a)]


def histogram_json(hist: Histogram, display_unit=None) -> dict:
    """ Bin edges, values and asymmetric errors of `hist` as lists, converted
    to `display_unit` if given. Bins that are upper limits (as for
    AsymmetricUncertainty.isupperlimit) have `is_limit` set and their 90%
    `upper_limit` given
    """
    values = hist.hist
    if display_unit is not None and hasattr(values, 'to'):
        values = values.to(display_unit)
    bins = hist.bin_edges
    m = getattr(values, 'm', values)
    value, lo, hi = _parts(m)
    is_limit = (value == 0) & (lo == 0) & (hi > 0)
    upper = np.broadcast_to(m.ppf(0.9), value.shape) if hasattr(m, 'ppf') \
        else value
    return dict(bins=_tolist(getattr(bins, 'm', bins)),
                value=_tolist(value), err_minus=_tolist(lo),
                err_plus=_tolist(hi),
                is_limit=[bool(x) for x in np.ravel(is_limit)],
                upper_limit=_tolist(upper),
                units=unit_str(getattr(values, 'u', None)),
                binsunit=unit_str(getattr(bins, 'u', None)))


def scalar_json(q, unit=None) -> Optional[dict]:
    """ Value and errors of a scalar quantity in `unit`. Upper limits have
    `is_limit` set and their 90% `upper_limit` given
    """
    if q is None:
        return None
    if unit is not None and hasattr(q, 'to'):
        q = q.to(unit)
    m = getattr(q, 'm', q)
    value, lo, hi = (float(x) for x in _parts(m))
    is_limit = bool(m.isupperlimit()) if hasattr(m, 'isupperlimit') else False
    upper = float(m.get_upper_limit(0.9)) if hasattr(m, 'get_upper_limit') \
        else value
    return dict(value=value, err_minus=lo, err_plus=hi, is_limit=is_limit,
                upper_limit=upper, units=unit_str(getattr(q, 'u', None)))
