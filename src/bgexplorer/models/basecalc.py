""" Calculate SourceTerms' scalar results without pint. Pint makes up most of
the time spent multiplying and adding quantities of thousands of SourceTerms,
though the units are the same for nearly all of them. Here the magnitudes are
converted to SI base units once per HitEfficiency, the units of each product
are worked out once per pair of units, and each SourceTerm is then only a
product of AsymmetricUncertainties. Pint is used again to convert sums to the
units they are displayed in (see `conversion_factor`).
"""
import logging
from typing import Dict, Optional
from .common import units as unitreg, DimensionalityError, Quantity
from .asymmetric import sum_values
from .hiteff import HitEfficiency
from .sourceterm import SourceTerm, _ref_id

log = logging.getLogger(__name__)
_DIMENSIONLESS = unitreg.Unit('')
_base_units = {}
_factors = {}
_products = {}


def base_units(unit) -> tuple:
    """ (factor, base unit): magnitudes in `unit` times factor are magnitudes
    in SI base units
    """
    try:
        return _base_units[unit]
    except KeyError:
        q = unitreg.Quantity(1., unit).to_base_units()
        return _base_units.setdefault(unit, (float(q.m), q.u))


def conversion_factor(base, unit) -> float:
    """ What to multiply magnitudes in the base units `base` by to get
    magnitudes in `unit`. Raises DimensionalityError if they don't match
    """
    try:
        return _factors[base, unit]
    except KeyError:
        factor = float(unitreg.Quantity(1., base).to(unit).m)
        return _factors.setdefault((base, unit), factor)


def _product(a, b):
    try:
        return _products[a, b]
    except KeyError:
        return _products.setdefault((a, b), a * b)


def _split(q):
    """ Magnitude and unit of quantity `q` """
    if isinstance(q, Quantity):
        return q.m, q.u
    return q, _DIMENSIONLESS


class ScalarCalculator:
    """ Calculates the scalars and ROIs of SourceTerms as plain magnitudes in
    SI base units, given the HitEfficiencies `hiteffs` they refer to (see
    `load_hiteffs`). One calculator is meant for a batch of SourceTerms, and
    is not thread safe.

    The base unit of each result is in `units`. If results of the same name
    have different dimensions, that name is in `mismatched` and its values
    can't be added up. `default_units` has a readable unit for each name, from
    the first SourceTerm that had it.
    """
    def __init__(self, hiteffs: Dict):
        self.hiteffs = hiteffs
        self.units = {}
        self.default_units = {}
        self.mismatched = set()
        self._hiteff_scalars = {}
        self._compatible = {}

    def _scalars(self, hiteff: HitEfficiency) -> list:
        """ (name, magnitude, unit, base unit) of each of `hiteff`'s scalars
        and ROIs, in base units
        """
        try:
            return self._hiteff_scalars[id(hiteff)][1]
        except KeyError:
            pass
        scalars = []
        for key, val in list(hiteff.scalars.items()) + list(
                hiteff.rois.items()):
            if val is None:
                continue
            m, unit = _split(val)
            factor, base = base_units(unit)
            scalars.append((key, m * factor, unit, base))
        # the id is only unique while hiteff is alive
        self._hiteff_scalars[id(hiteff)] = (hiteff, scalars)
        return scalars

    def _check_norm(self, hiteff: HitEfficiency, base) -> None:
        key = hiteff.norm, base
        try:
            ok = self._compatible[key]
        except KeyError:
            rate = None if base is None else unitreg.Quantity(1., base)
            ok = self._compatible[key] = bool(hiteff.norm.check(rate))
        if not ok:
            raise DimensionalityError("incompatible emissionrate units")

    @staticmethod
    def _rate(sourceterm: SourceTerm):
        """ The emission rate's magnitude, scaled to base units, and its
        unit, or None
        """
        rate = sourceterm.unweighted_rate
        if rate is None:
            return None
        m, unit = rate.m, rate.u
        scale = sourceterm.weight
        multiplier = sourceterm.rate_multiplier
        if isinstance(multiplier, Quantity):
            m = m * multiplier.m
            unit = unit * multiplier.u
        elif multiplier is not None:
            scale = scale * multiplier
        factor, base = base_units(unit)
        return m * (scale * factor), unit, base

    def calculate(self, sourceterm: SourceTerm) -> Optional[dict]:
        """ Multiply the sourceterm's emission rate by each of its hiteffs,
        which are looked up in the `hiteffs` this was created with. Returns a
        dict of the sum of each scalar, or None if there are no hiteffs
        """
        parts = {}
        rate = None
        found = False
        for ref in sourceterm._data.get('hiteffs') or []:
            hiteff = self.hiteffs.get(_ref_id(ref))
            if hiteff is None:
                if not isinstance(ref, HitEfficiency):
                    log.warning(f"HitEfficiency {_ref_id(ref)} for "
                                f"SourceTerm {sourceterm.id} not found")
                    continue
                hiteff = ref
            if not found:
                rate = self._rate(sourceterm)
            found = True
            rate_m, rate_unit, rate_base = rate or (None, None, None)
            self._check_norm(hiteff, rate_base)
            for key, m, unit, base in self._scalars(hiteff):
                if rate is None:
                    continue
                product = _product(base, rate_base)
                if self.units.setdefault(key, product) != product:
                    self.mismatched.add(key)
                if key not in self.default_units:
                    self.default_units[key] = unitreg.Quantity(
                        1., unit * rate_unit).to_reduced_units().u
                parts.setdefault(key, []).append(m * rate_m)
        if not found:
            return None
        return {key: sum_values(values) for key, values in parts.items()}
