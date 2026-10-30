""" Calculate the results of SourceTerms in the units they are displayed in.

Every scalar, ROI and spectrum has a display unit in the version's settings,
for the hit efficiency's value times its norm (see `HitEffDbConfig`). Each
SourceTerm's results are converted to those units once, as they are
calculated, so everything built from them (sums, breakdowns, tables) is plain
arithmetic on AsymmetricUncertainties. Pint is only used once per hit
efficiency and rate unit, to work out the conversion factor, which is
folded into the hit efficiency's values.
"""
import logging
from typing import Dict, Optional, Tuple
from pint.errors import PintError
from .common import units as unitreg, Quantity, UnitMismatchError
from .asymmetric import sum_values
from .histogram import Histogram
from .hiteff import HitEfficiency
from .verdoc import ref_id as _ref_id

log = logging.getLogger(__name__)
_DIMENSIONLESS = unitreg.Unit('')
_factors = {}


def conversion_factor(base, unit) -> float:
    """ What to multiply magnitudes in the unit `base` by to get magnitudes in
    `unit`. Raises DimensionalityError if they don't match
    """
    try:
        return _factors[base, unit]
    except KeyError:
        factor = float(unitreg.Quantity(1., base).to(unit).m)
        return _factors.setdefault((base, unit), factor)


def _split(q):
    """ Magnitude and unit of quantity `q` """
    if isinstance(q, Quantity):
        return q.m, q.u
    return q, _DIMENSIONLESS


class TermCalculator:
    """ Calculates the scalars, ROIs and spectra of SourceTerms in the display
    units of the settings `config` (a HitEffDbConfig), given the
    HitEfficiencies `hiteffs` they refer to (see `load_hiteffs`). One
    calculator is meant for a batch of SourceTerms, and is not thread safe.

    SourceTerms only match hit efficiencies whose norm fits their rate, and
    the units of both are checked when they are saved, so the results always
    fit the display units. Raises UnitMismatchError if they don't, or have
    none, which means data was written without being validated.
    """
    def __init__(self, config, hiteffs: Optional[Dict] = None):
        self.config = config
        self.hiteffs = hiteffs or {}
        self._scaled = {}

    def _hiteffs(self, sourceterm):
        """ The HitEfficiencies of `sourceterm` """
        for ref in sourceterm._data.get('hiteffs') or []:
            hiteff = self.hiteffs.get(_ref_id(ref))
            if hiteff is None:
                if not isinstance(ref, HitEfficiency):
                    log.warning(f"HitEfficiency {_ref_id(ref)} for "
                                f"SourceTerm {sourceterm.id} not found")
                    continue
                hiteff = ref
            yield hiteff

    @staticmethod
    def _rate(sourceterm) -> Optional[Tuple]:
        """ The emission rate's magnitude and unit, or None """
        rate = sourceterm.source.standardized_rate
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
        return m * scale, unit

    @staticmethod
    def _describe(hiteff) -> str:
        return (f"HitEfficiency {hiteff.id} ({hiteff.source} at "
                f"{hiteff.location})")

    def _factor(self, hiteff, key: str, unit, display) -> float:
        """ The factor to convert `key` of `hiteff`, which has `unit` once
        multiplied by the rate, to `display`
        """
        if display is None:
            raise UnitMismatchError(f"{key} has no display unit in the "
                                    f"settings, needed by "
                                    f"{self._describe(hiteff)}")
        try:
            return conversion_factor(unit, display)
        except PintError as e:
            raise UnitMismatchError(
                f"{key} of {self._describe(hiteff)}, times the emission "
                f"rate, is in {unit}, which is not the display unit "
                f"{display}") from e

    def _scaled_scalars(self, hiteff, rate_unit) -> list:
        """ (key, magnitude) of each of `hiteff`'s scalars and ROIs, in
        display units once multiplied by a rate magnitude in `rate_unit`
        """
        cache_key = 'scalars', id(hiteff), rate_unit
        try:
            return self._scaled[cache_key][1]
        except KeyError:
            pass
        scaled = []
        for key, val in hiteff.all_scalars.items():
            if val is None:
                continue
            m, unit = _split(val)
            display = self.config.scalar_unit(key)
            factor = self._factor(hiteff, key, unit * rate_unit, display)
            scaled.append((key, m if factor == 1. else m * factor))
        # the id is only unique while hiteff is alive
        self._scaled[cache_key] = (hiteff, scaled)
        return scaled

    def _scaled_spectra(self, hiteff, rate_unit) -> list:
        """ (key, Histogram) of each of `hiteff`'s spectra, in display units
        once multiplied by a rate magnitude in `rate_unit`
        """
        cache_key = 'spectra', id(hiteff), rate_unit
        try:
            return self._scaled[cache_key][1]
        except KeyError:
            pass
        scaled = []
        for key, val in hiteff.spectra.items():
            if not isinstance(val, Histogram):
                continue
            m, unit = _split(val.hist)
            display = self.config.spectrum_unit(key)
            factor = self._factor(hiteff, key, unit * rate_unit, display)
            scaled.append((key, Histogram(
                unitreg.Quantity(m if factor == 1. else m * factor, display),
                val.bin_edges)))
        self._scaled[cache_key] = (hiteff, scaled)
        return scaled

    def scalars(self, sourceterm) -> dict:
        """ The sourceterm's scalars and ROIs, summed over its hit
        efficiencies, as AsymmetricUncertainties in display units
        """
        rate = self._rate(sourceterm)
        if rate is None:
            return {}
        rate_m, rate_unit = rate
        parts = {}
        for hiteff in self._hiteffs(sourceterm):
            for key, m in self._scaled_scalars(hiteff, rate_unit):
                parts.setdefault(key, []).append(m * rate_m)
        return {key: sum_values(values) for key, values in parts.items()}

    def spectra(self, sourceterm) -> dict:
        """ The sourceterm's spectra, summed over its hit efficiencies, as
        Histograms in display units
        """
        rate = self._rate(sourceterm)
        if rate is None:
            return {}
        rate_m, rate_unit = rate
        parts = {}
        for hiteff in self._hiteffs(sourceterm):
            for key, hist in self._scaled_spectra(hiteff, rate_unit):
                parts.setdefault(key, []).append(Histogram(
                    unitreg.Quantity(hist.hist.m * rate_m, hist.hist.u),
                    hist.bin_edges))
        return {key: Histogram.sum_all(values)
                for key, values in parts.items()}
