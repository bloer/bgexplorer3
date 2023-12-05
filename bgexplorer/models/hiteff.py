from mongoengine import Document, DynamicDocument, EmbeddedDocument
from mongoengine.fields import (StringField, MapField, UUIDField, ListField,
                                SortedListField, IntField, DynamicField,
                                EnumField, BooleanField, DateTimeField,
                                FloatField, DictField, EmbeddedDocumentField)
from mongoengine.errors import ValidationError
from mongoengine.context_managers import switch_collection
import bson
from .verdoc import DynamicVersionedDocument
from .fields import (QuantityField, UncertainQuantityField, HistogramField,
                     UnitField)
from .common import units as unitreg
from typing import Union, Optional
from enum import Enum
import logging
import datetime
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
        return unitreg(unitstr)

    def check(self, emissionrate):
        """ check if emissionrate has the right units """
        return self.units.check(emissionrate)


class HitEfficiency(DynamicVersionedDocument):
    """ Document describing how efficiently radiation from a given source at
    a given location is converted to hits in the sensitive detector. Usually
    these are produced by Monte Carlo simulations. This is a DynamicDocument
    so any metadata is permitted

    Required attributes:
       source (str): What is the radiation source? Match name from assay, etc.
       location (str): Where the source is located relative to the detector
                       usually the name of a MC volume
       distribution (str): whether the source is distributed in the bulk,
                           on a surface, or some other way
       norm (NormMultiplier): To convert HitEfficiency to rates in detector,
                              multiply by emission rate in some units,
                              usually 'rate' (decays/s), but can be
                              'flux' (primaries/s/cm**2)
                              'flux_per_sr' (primaries/s/cm**2/sr) or
                              'none' (HitEff is absolutely normalized)
       values (dict): Single-value hit efficiencies (as asym. uncertainties
                      with units), such as integral counts over some ROI
       spectra (dict): histograms of hit efficiencies

    Suggested metadata:
       nprimaries: number of primary particles simulated
       primary_spectrum: filename or representation of the spectrum of
                         particles thrown e.g. simulating (alpha,n) neutrons
       primary_particle: name of primary particle
       primary_yield: when simulating e.g. neutrons or equilibrium gammas, the
              average neutrons or gammas emitted per parent isotope decay
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

    Queries against the database are made against (source, location, distr.).
    Multiple responses are grouped by (primary_particle, primary_spectrum).
    So e.g.
    """
    # required metadata and results
    source = StringField(required=True)
    location = StringField(required=True)
    distribution = StringField(required=False, default='bulk')
    norm = EnumField(NormMultiplier, required=False,
                     default=NormMultiplier.rate)
    values = MapField(UncertainQuantityField(allownone=True),
                      required=False, default=dict)
    spectra = MapField(HistogramField(allownone=True),
                       required=False, default=dict)

    # optional but suggested metadata
    nprimaries = IntField(required=False)
    primary_particle = StringField(required=False)
    primary_spectrum = DynamicField(required=False)
    primary_yield = FloatField(required=False, default=1)
    biasweight = FloatField(required=False, default=1)
    livetime = QuantityField(units='s', required=False)
    version = DynamicField(required=False)
    files = SortedListField(StringField(), required=False)
    uuids = SortedListField(UUIDField(), required=False)
    date = DynamicField(required=False)
    metadata = DictField()

    # for internal use
    values_keys = ListField(StringField())
    spectra_keys = ListField(StringField())

    meta = {
        'indexes': ['location', 'distribution', 'source', 'version', 'date',
                    'values_keys', 'spectra_keys']
    }

    def __init__(self, *args, **kwargs):
        """ Set an ID on all values and spectra to track correlations """
        super().__init__(*args, **kwargs)
        for key, val in self.values.items():
            if val is not None:
                val.m.id = '.'.join([str(self.id), 'v', key])
        for key, val in self.spectra.items():
            if val is not None:
                val.hist.m.id = '.'.join([str(self.id), 's', key])
        # use a post-init signal

    def clean(self):
        super().clean()
        self.values_keys = list(self.values)
        self.spectra_keys = list(self.spectra)

    @property
    def key(self):
        getattr(self, '_id', (self.source, self.location, self.distribution))

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
                    (emissionrate * self.primary_yield))
        except (AttributeError, TypeError):
            # nprimaries not provided
            pass
        except Exception:
            log.error("Error calculating livetime for HitEfficiency %s",
                      self.key)
        return None
