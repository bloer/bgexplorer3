from mongoengine import (StringField, EnumField, ValidationError,
                         ObjectIdField, EmbeddedDocument,
                         EmbeddedDocumentListField,
                         )
from bson import ObjectId
from enum import Enum
from typing import Optional, Union
from .common import units
from .isotope import (concentration_to_rate, rate_to_concentration,
                      get_isotope, compare_source_names)
from .fields import UncertainQuantityField
from .verdoc import VersionedDocument
from . import settings
from collections.abc import Mapping


class Multiplier(Enum):
    mass = 'mass'
    volume = 'volume'
    surface = 'surface_area'
    inner_surface = 'inner_surface_area'
    outer_surface = 'outer_surface_area'
    length = 'length'
    none = 'none'

    @classmethod
    def get_multiplier(cls, rate: Optional[units.Quantity]) -> 'Multiplier':
        """ For rate specified in given units, find the appropriate
        multiplier. Raise ValidationError if multiplier can't be determined
        """
        if rate is None:
            return Multiplier.none
        # TODO: this will return mass for any dimensionless rate
        # should instead explicitly check for ppm, ppb, ppt
        if units('Bq/kg').check(rate) or units('ppb').check(rate):
            return cls.mass
        elif units('Bq/m**3').check(rate):
            return cls.volume
        elif units('Bq/m**2').check(rate):
            return cls.surface
        elif units('Bq').check(rate):
            return cls.none
        elif units('1/cm**2/s/sr').check(rate):
            return cls.none
        elif units('Bq/m').check(rate):
            return cls.length
        raise ValidationError(f"Unhandled rate units {rate.u}")

    @property
    def valid_units(self):
        """ Get a list of valid units for this multiplier """
        return {
                Multiplier.mass: ['Bq/kg', 'ppb'],
                Multiplier.volume: ['Bq/m**3'],
                Multiplier.surface: ['Bq/m**2'],
                Multiplier.inner_surface: ['Bq/m**2'],
                Multiplier.outer_surface: ['Bq/m**2'],
                Multiplier.length: ['Bq/m'],
                Multiplier.none: ['Bq', '1/cm**2/s', '1/cm**2/s/sr'],
                }[self]

    def check_units(self, rate: units.Quantity) -> None:
        """ Verify that `rate` has appropriate units for us, raise
        ValidationError otherwise
        """
        if rate is None:
            return
        for u in self.valid_units:
            if units(u).check(rate):
                return True
        # if we get here, none match
        raise ValidationError(f"Rate units {rate.u} invalid"
                              f" for multiplier {self}")

    @property
    def default_distribution(self) -> str:
        return {Multiplier.mass: 'bulk',
                Multiplier.volume: 'bulk',
                Multiplier.surface: 'surface',
                Multiplier.inner_surface: 'inner_surface',
                Multiplier.outer_surface: 'outer_surface',
                Multiplier.length: 'bulk',
                Multiplier.none: 'bulk',
                None: None}[self]

    def determine_distribution(self, component):
        if component.distribution:
            return component.distribution
        dist = self.default_distribution
        if component.treat_surface_as_bulk and dist.find('surface') != -1:
            dist = 'bulk'
        return dist

    def getvalue(self, component) -> Union[float, units.Quantity]:
        """ Extract the numerical value of the multipler from component """
        mult = 1
        if self is not Multiplier.none:
            mult = getattr(component, self.name)
        return mult


class SourceCategory(Enum):
    target = 'target'
    estimate = 'estimate'
    assay = 'assay'
    activation = 'activation'
    dust = 'dust'
    radon = 'radon'


class EmissionSource(EmbeddedDocument):
    id = ObjectIdField(default=ObjectId)
    name = StringField(required=True)
    comment = StringField()
    category = EnumField(SourceCategory)
    rate = UncertainQuantityField(allownone=True)
    multiplier = EnumField(Multiplier)
    particle = StringField()
    spectrum = StringField()
    generated_from = ObjectIdField()

    def __init__(self, *args, **kwargs):
        """ Set id on rate so that correlations are tracked appropriately """
        super().__init__(*args, **kwargs)
        if self.rate is not None:
            self.rate.m.id = self.id

    def clean(self):
        """ make sure multiplier has a sensible value """
        if self.multiplier is None:
            self.multiplier = Multiplier.get_multiplier(self.rate)
            # ^ will raise ValidationError if it can't be auto-determined
        else:
            self.multiplier.check_units(self.rate)

        if (self.multiplier is Multiplier.mass and
                self.rate.check('ppb') and
                get_isotope(self.name) is None):

            self.error("to specify rate as concentration, `name` must be "
                       "an isotope")


class EmissionSpec(VersionedDocument):
    name = StringField(required=True)
    description = StringField()
    comment = StringField()
    category = EnumField(SourceCategory)
    sources = EmbeddedDocumentListField(EmissionSource)

    meta = {'allow_inheritance': True,
            'indexes': ['sources.name'],
            }

    # __slots__ = ['sourcemap']

    def __init__(self, *args, **kwargs):
        """ shortcut to provide sources as {name: rate} mapping """
        if isinstance(sources := kwargs.get('sources'), Mapping):
            kwargs['sources'] = [EmissionSource(name=k, rate=v)
                                 for k, v in sources.items()]
        super().__init__(*args, **kwargs)
        self.sourcemap = {s.name: s for s in self.sources}

    def get_sources(self, component):
        return self.sources

    def _addsource(self, newsource: settings.AddSource) -> None:
        """ Add or update sources from the VersionSettings/AddSource list """
        matchin = None
        matchout = None
        for source in self.sources:
            if compare_source_names(newsource.source, source.name):
                matchin = source

        if matchin is None:
            return

        matchout = [s for s in self.sources if s.generated_from == matchin.id
                    and compare_source_names(newsource.newsource, s.name)]
        doinsert = False
        if matchout:
            matchout = matchout[0]
        else:
            doinsert = True
            matchout = EmissionSource(name=newsource.newsource,
                                      generated_from=matchin.id)

        matchout.multiplier = matchin.multiplier
        rate = matchin.rate * newsource.ratio
        rate_is_concentration = (units('ppb').check(matchin.rate) or
                                 units('g').check(matchin.rate))
        if (rate_is_concentration and
                newsource.ratiotype is settings.RatioType.rate):
            rate = concentration_to_rate(matchin.name, matchin.rate)
            rate = rate_to_concentration(matchout.name,
                                         rate * newsource.ratio)
        elif (not rate_is_concentration and
              newsource.ratiotype is settings.RatioType.abundance):
            rate = rate_to_concentration(matchin.name, matchin.rate)
            rate = concentration_to_rate(matchout.name, rate * newsource.ratio)
        matchout.rate = rate.to(matchin.rate.u)

        if doinsert:
            self.sources.append(matchout)

    def clean(self):
        """ Automatically populate derived spectra from settings """
        super().clean()
        # TODO: error checking on active_version and config validity
        config = settings.get_settings(self.active_version)
        # remove all auto-generated sources that no longer have an original
        for source in list(self.sources):
            if not source.generated_from:
                continue
            if len([s for s in self.sources
                    if s.id == source.generated_from]) == 0:
                self.sources.remove(source)
        # add new sources
        for source in config.addsources:
            self._addsource(source)

        # update category
        for source in self.sources:
            source.category = source.category or self.category
        self.sourcemap = {s.name: s for s in self.sources}

