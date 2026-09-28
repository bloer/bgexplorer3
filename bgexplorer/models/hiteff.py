from mongoengine.fields import (StringField, MapField, UUIDField, ListField,
                                SortedListField, IntField, DynamicField,
                                EnumField, FloatField, DictField)
from mongoengine.errors import ValidationError
from .verdoc import DynamicVersionedDocument
from .fields import QuantityField, UncertainQuantityField, HistogramField
from .common import units as unitreg
from .histogram import Histogram
from . import settings
from enum import Enum
from itertools import chain
import logging
log = logging.getLogger(__name__)


dbalias = "hiteffdb"


class NormMultiplier(Enum):
    rate = 'rate'
    flux = 'flux'
    flux_per_sr = 'flux_per_sr'
    none = 'none'

    @property
    def units(self):
        unitstr = None
        if self is NormMultiplier.rate:
            unitstr = '1/s'
        elif self is NormMultiplier.flux:
            unitstr = '1/s/cm**2'
        elif self is NormMultiplier.flux_per_sr:
            unitstr = '1/s/cm**2/sr'
        return unitreg(unitstr).u

    def check(self, emissionrate):
        """ check if emissionrate has the right units """
        return self.units.is_compatible_with(emissionrate)


class HitEfficiency(DynamicVersionedDocument):
    """ Document describing how efficiently radiation from a given source at
    a given location is converted to hits in the sensitive detector. Usually
    these are produced by Monte Carlo simulations. This is a DynamicDocument
    so any metadata is permitted

    Required attributes:
       source (str): What is the radiation source? Match name from assay, etc.
       location (str): Where the source is located relative to the detector
                       usually the name of a MC volume
       norm (NormMultiplier): To convert HitEfficiency to rates in detector,
                              multiply by emission rate in some units,
                              usually 'rate' (decays/s), but can be
                              'flux' (primaries/s/cm**2)
                              'flux_per_sr' (primaries/s/cm**2/sr) or
                              'none' (HitEff is absolutely normalized)
       scalars (dict): Single-value hit efficiencies (as asym. uncertainties
                      with units), such as integral counts over some ROI
       spectra (dict): histograms of hit efficiencies

    Suggested metadata:
       nprimaries: number of primary particles simulated
       primary_spectrum: filename or representation of the spectrum of
                         particles thrown e.g. simulating (alpha,n) neutrons
       primary_particle: name of primary particle
       primary_yield: when simulating e.g. neutrons or equilibrium gammas, the
              average neutrons or gammas emitted per parent isotope decay
       material: only match components made of this material, e.g. for
                 (alpha,n) neutron yields
       biasweight: any biasing applied to the simulation
       livetime: In rare cases the simulation or spectrum is absolutely
                 normalized, e.g. coherent neutrino backgrounds or dark current
                 In this case livetime can be recorded rather than calculated
       version: software version information
       files: filenames used for calculation
       uuids: UUIDs of files used
       date: date entry was created

    If nprimaries is provided, the simulation livetime will be displayed where
    appropriate as (nprimaries*biasweight / (emissionrate*yield))

    Queries against the database are made against (source, location,
    material). A HitEfficiency with no material matches any material.
    Multiple responses are grouped by (primary_particle, primary_spectrum).
    """
    # required metadata and results
    source = StringField(required=True)
    location = StringField(required=True)
    norm = EnumField(NormMultiplier, required=False,
                     default=NormMultiplier.rate)
    scalars = MapField(UncertainQuantityField(allownone=True),
                      required=False, default=dict)
    spectra = MapField(HistogramField(allownone=True),
                       required=False, default=dict)
    rois = MapField(UncertainQuantityField(allownone=True),
                    required=False, default=dict)

    # optional but suggested metadata
    nprimaries = IntField(required=False)
    primary_particle = StringField(required=False)
    primary_spectrum = DynamicField(required=False)
    primary_yield = FloatField(required=False, default=1)
    primary_area = QuantityField(required=False, units='cm**2')
    primary_volume = QuantityField(required=False, units='cm**3')
    material = StringField(default=None)
    biasweight = FloatField(required=False, default=1)
    livetime = QuantityField(units='s', required=False)
    version = DynamicField(required=False)
    files = SortedListField(StringField(), required=False)
    uuids = SortedListField(UUIDField(), required=False)
    date = DynamicField(required=False)
    extra_metadata = DictField()

    # for internal use
    scalars_keys = ListField(StringField())
    spectra_keys = ListField(StringField())

    meta = {
        'indexes': ['location', 'source', 'version', 'date',
                    'scalars_keys', 'spectra_keys']
    }

    def __init__(self, *args, **kwargs):
        """ Set an ID on all scalars and spectra to track correlations """
        super().__init__(*args, **kwargs)
        for key, val in chain(self.scalars.items(), self.rois.items()):
            if val is not None:
                val.m.id = '.'.join([str(self.id), 'v', key])
        for key, val in self.spectra.items():
            if val is not None:
                val.hist.m.id = '.'.join([str(self.id), 's', key])
        # use a post-init signal?

    def check_dbconfig(self, dbconfig):
        """ Make sure we are compatible with the HitEffDBconfig
        Raise ValidationError if not. Scalars or spectra that weren't loaded
        from the database are left alone.
        """
        # make sure units match the desired output
        for type_ in ('scalars', 'spectra'):
            if not self.is_loaded(type_):
                continue
            register = getattr(self, type_)
            for key, val in register.items():
                display_register = getattr(dbconfig, f'display_{type_}')
                if val is None or key not in display_register:
                    continue
                display_unit = display_register[key].display_unit
                if display_unit is None:
                    continue
                if not display_unit.is_compatible_with(self.get_result_unit(val)):
                    msg = f"{type_} {key} {val} has incorrect units"
                    raise ValidationError(msg)

        # evaluate all ROIs
        if not self.is_loaded('spectra'):
            return
        self.rois = dict()
        for roi in dbconfig.rois:
            roi.evaluate(self, store=True)

    def clean(self):
        super().clean()
        dbconfig = settings.get_settings(self.active_version).hiteffdbconfig
        self.check_dbconfig(dbconfig)
        if self.is_loaded('scalars'):
            self.scalars_keys = list(self.scalars)
        if self.is_loaded('spectra'):
            self.spectra_keys = list(self.spectra)

    def __str__(self):
        return f"{self.source} - {self.location}"

    def _check_spectra_loaded(self):
        if not self.is_loaded('spectra'):
            raise ValueError("Spectra were not loaded for this document")

    def add_spectrum(self, name: str, hist: Histogram,
                     overwrite: bool = False) -> None:
        """ Add spectrum `name` and save. Raises KeyError if it exists and
        not `overwrite`, ValidationError if units don't match the settings
        """
        self._check_spectra_loaded()
        name = name.strip()
        if not name:
            raise ValueError("A spectrum name is required")
        if name in self.spectra and not overwrite:
            raise KeyError(f"Spectrum '{name}' already exists")
        spectra = dict(self.spectra)
        spectra[name] = hist
        self.spectra = spectra
        self.save()

    def rename_spectrum(self, old: str, new: str) -> None:
        """ Rename spectrum `old` to `new` and save """
        self._check_spectra_loaded()
        new = new.strip()
        if old not in self.spectra:
            raise KeyError(f"No spectrum '{old}'")
        if not new:
            raise ValueError("A spectrum name is required")
        if new == old:
            return
        if new in self.spectra:
            raise KeyError(f"Spectrum '{new}' already exists")
        self.spectra = {new if k == old else k: v
                        for k, v in self.spectra.items()}
        self.save()

    def remove_spectrum(self, name: str) -> None:
        """ Delete spectrum `name` and save """
        self._check_spectra_loaded()
        if name not in self.spectra:
            raise KeyError(f"No spectrum '{name}'")
        self.spectra = {k: v for k, v in self.spectra.items() if k != name}
        self.save()

    @property
    def key(self):
        return self.id or (self.source, self.location)

    def get_result_unit(self, val):
        """ Determine the output unit for a value from scalars or spectra
        based on our norm value
        """
        if isinstance(val, Histogram):
            val = val.hist
        try:
            return (1 * val.u * self.norm.units).to_reduced_units().u
        except AttributeError:
            return None

    def check_result_unit(self, val, unit):
        """ Test whether the units we expect for output are compatible with
        `unit`, e.g. a user-selected display unit
        """
        if unit is None:
            return True
        if isinstance(unit, str):
            unit = unitreg(unit).u
        if result_unit := self.get_result_unit(val):
            return result_unit.is_compatible_with(unit)
        return True

    def get_livetime(self, emissionrate=None):
        if self.livetime is not None:
            return self.livetime
        # discard uncertainties
        try:
            if emissionrate.isupperlimit():
                emissionrate = emissionrate.get_upper_limit() * emissionrate.u
            else:
                emissionrate = emissionrate.nominal_value * emissionrate.u
        except AttributeError:
            # emissionrate is None
            pass

        try:
            return (self.nprimaries * self.biasweight /
                    (emissionrate * self.primary_yield)).to('second')
        except (AttributeError, TypeError):
            # nprimaries not provided
            pass
        except Exception:
            log.error("Error calculating livetime for HitEfficiency %s "
                      "(%s, %s)", self.key, self.source, self.location)
        return None
