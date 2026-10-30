from mongoengine import (StringField, EnumField, ValidationError,
                         ObjectIdField, EmbeddedDocument,
                         EmbeddedDocumentListField,
                         )
from bson import ObjectId
from enum import Enum
from typing import Optional, Union
from .common import units, validate_unique_ids
from .isotope import (concentration_to_rate, rate_to_concentration,
                      compare_source_names, is_radioactive)
from .fields import UncertainQuantityField, AttachmentsField
from .verdoc import VersionedDocument, VersionedReferenceField
from . import settings
from collections.abc import Mapping


class Multiplier(Enum):
    """ The property of a component that a source's rate is multiplied by """
    mass = 'mass'
    volume = 'volume'
    surface = 'surface_area'
    inner_surface = 'inner_surface_area'
    outer_surface = 'outer_surface_area'
    length = 'length'
    flux = 'flux'
    flux_per_sr = 'flux_per_sr'
    per_piece = 'per_piece'

    @classmethod
    def get_multiplier(cls, rate: Optional[units.Quantity],
                       allow_mass_to_rate: bool = False
                       ) -> Optional['Multiplier']:
        """ For rate specified in given units, find the appropriate
        multiplier, also for a mass of an isotope if `allow_mass_to_rate`
        (see `check_units`). Raise ValidationError if multiplier can't be
        determined. Fluxes have the dimensions of surface rates, so are
        never chosen
        """
        if rate is None:
            return None
        for allow in dict.fromkeys((False, allow_mass_to_rate)):
            for mult in (cls.mass, cls.volume, cls.surface, cls.length,
                         cls.per_piece):
                if mult.accepts(rate, allow):
                    return mult
        raise ValidationError(f"Unhandled rate units {rate.u}")

    @property
    def valid_units(self) -> units.Unit:
        """ The units of rates for this multiplier """
        return _VALID_UNITS[self]

    def check_units(self, rate: Optional[units.Quantity],
                    allow_mass_to_rate: bool = True) -> None:
        """ Verify that `rate` has appropriate units for us, raise
        ValidationError otherwise. If `allow_mass_to_rate`, allow being off
        by a factor of 1/g/s, because a mass of an isotope can be converted
        to a decay rate.
        """
        if rate is None:
            return
        dims = self.valid_units.dimensionality
        if rate.u.dimensionality == dims:
            return
        if (allow_mass_to_rate and
                (rate.u * _MASS_TO_RATE).dimensionality == dims):
            return
        raise ValidationError(f"Rate units {rate.u} invalid"
                              f" for multiplier {self.name}")

    def accepts(self, rate: Optional[units.Quantity],
                allow_mass_to_rate: bool = True) -> bool:
        """ Does `rate` have appropriate units for us? """
        try:
            self.check_units(rate, allow_mass_to_rate)
        except ValidationError:
            return False
        return True

    def getvalue(self, component) -> Union[float, units.Quantity]:
        """ Extract the numerical value of the multipler from component """
        if self in _UNIT_MULTIPLIERS:
            return 1
        # values name the Component attributes, e.g. surface_area
        return getattr(component, self.value)


_VALID_UNITS = {
    Multiplier.mass: units.Unit('Bq/kg'),
    Multiplier.volume: units.Unit('Bq/m**3'),
    Multiplier.surface: units.Unit('Bq/m**2'),
    Multiplier.inner_surface: units.Unit('Bq/m**2'),
    Multiplier.outer_surface: units.Unit('Bq/m**2'),
    Multiplier.length: units.Unit('Bq/m'),
    Multiplier.flux: units.Unit('1/cm**2/s'),
    Multiplier.flux_per_sr: units.Unit('1/cm**2/s/sr'),
    Multiplier.per_piece: units.Unit('Bq'),
}
# multipliers that don't scale with the component
_UNIT_MULTIPLIERS = (Multiplier.flux, Multiplier.flux_per_sr,
                     Multiplier.per_piece)
# a mass of an isotope times this has the dimensions of its decay rate
_MASS_TO_RATE = units.Unit('1/g/s')


class SourceCategory(Enum):
    target = 'target'
    assay = 'assay'
    activation = 'activation'
    dust = 'dust'
    radon = 'radon'
    radon_emanation = 'radon_emanation'
    environment = 'environment'
    other = 'other'


class EmissionSource(EmbeddedDocument):
    id = ObjectIdField(required=True, default=ObjectId)
    name = StringField(required=True)
    comment = StringField()
    category = EnumField(SourceCategory)
    # computed rates stay correlated with what they're computed from
    rate = UncertainQuantityField(allownone=True, expressions='inline')
    multiplier = EnumField(Multiplier)
    particle = StringField()
    spectrum = StringField()
    generated_from = ObjectIdField()

    @property
    def converts_mass(self) -> bool:
        """ Can our rate be given as a mass, e.g. a concentration? Only if we
        are a radioactive isotope
        """
        return is_radioactive(self.name)

    @property
    def standardized_rate(self) -> Optional[units.Quantity]:
        """ The rate in the units of our multiplier, converting a mass of an
        isotope to its decay rate. None if we have no rate
        """
        if self.rate is None:
            return None
        if self.multiplier is None:
            raise ValueError("Multiplier is not set")
        if self.multiplier.accepts(self.rate, allow_mass_to_rate=False):
            return self.rate
        return concentration_to_rate(self.name, self.rate)

    def clean(self):
        """ Choose the multiplier from the rate's units if it isn't set, and
        make sure the rate fits it. A radioactive isotope's rate may be given
        as a mass of it, e.g. a concentration
        """
        if self.rate is None:
            return
        converts = self.converts_mass
        try:
            if self.multiplier is None:
                self.multiplier = Multiplier.get_multiplier(self.rate,
                                                            converts)
            self.multiplier.check_units(self.rate, converts)
        except ValidationError as e:
            candidates = [self.multiplier] if self.multiplier else Multiplier
            if not converts and any(m.accepts(self.rate)
                                    for m in candidates):
                raise ValidationError(
                    "to give the rate as a mass, e.g. a concentration, the "
                    "name must be a radioactive isotope",
                    field_name='name') from e
            # the rate is what's wrong, so forms can show it there
            raise ValidationError(e.message, field_name='rate') from e

    def __str__(self):
        return f"EmissionSource({self.name}, {self.rate})"

class EmissionSpec(VersionedDocument):
    name = StringField(required=True)
    description = StringField()
    comment = StringField()
    category = EnumField(SourceCategory)
    multiplier = EnumField(
        Multiplier,
        help_text="Used for every source without its own, e.g. to choose "
                  "inner or outer surface")
    sources = EmbeddedDocumentListField(EmissionSource)
    attachments = AttachmentsField()
    # specs belonging to one component, e.g. its own exposure history, are
    # only attached to it and hidden from lists of shared specs
    owner = VersionedReferenceField('Component', endpoint='component',
                                    help_text="The only component that uses "
                                              "this spec")

    meta = {'allow_inheritance': True,
            'indexes': ['sources.name', 'owner'],
            }

    # __slots__ = ['sourcemap']

    def __init__(self, *args, **kwargs):
        """ shortcut to provide sources as {name: rate} mapping """
        if isinstance(sources := kwargs.get('sources'), Mapping):
            kwargs['sources'] = [EmissionSource(name=k, rate=v)
                                 for k, v in sources.items()]
        super().__init__(*args, **kwargs)
        self.sourcemap = {s.name: s for s in self.sources}

    def __str__(self):
        # return self.name or f"new {type(self).__name__}"
        return f"EmissionSpec({self.name}, { {source.name: str(source.rate) for source in self.sources} })"

    def get_sources(self, component):
        return self.sources

    def _addsource(self, newsource: settings.AddSource) -> None:
        """ Add or update sources from the VersionSettings/AddSource list """
        matchin = None
        matchout = None
        for source in self.sources:
            if compare_source_names(newsource.source, source.name):
                matchin = source

        if matchin is None or matchin.rate is None:
            return

        generated = [s for s in self.sources if s.generated_from == matchin.id
                     and compare_source_names(newsource.newsource, s.name)]
        # generated sources are defaults: a source given by the user wins
        if any(s is not matchin and not s.generated_from
               and compare_source_names(newsource.newsource, s.name)
               for s in self.sources):
            for source in generated:
                self.sources.remove(source)
            return

        matchout = generated
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

    # subclasses that calculate their sources set this, so they aren't edited
    COMPUTED_SOURCES = False

    # fields of an EmissionSource which, if edited, make a generated source
    # an override of the default
    OVERRIDE_FIELDS = ('name', 'category', 'rate', 'multiplier', 'particle',
                       'spectrum')

    def _set_computed_sources(self, sources) -> None:
        """ For subclasses with COMPUTED_SOURCES: replace the sources that
        weren't generated by the settings with `sources`. Each keeps the id
        of the old source with the same name, so correlations, generated
        sources and anything else keyed to it stay the same
        """
        old = [s for s in self.sources if not s.generated_from]
        for source in sources:
            match = next((s for s in old
                          if compare_source_names(s.name, source.name)), None)
            if match is not None:
                source.id = match.id
                old.remove(match)
        self.sources = sources + [s for s in self.sources if s.generated_from]

    def mark_overrides(self, edited) -> None:
        """ Generated sources whose (str) id is in `edited` were changed by
        the user, so keep their values instead of regenerating them
        """
        for source in self.sources:
            if source.generated_from and str(source.id) in edited:
                source.generated_from = None

    def clean(self):
        """ Automatically populate derived spectra from settings """
        # sources without their own category or multiplier use ours, which
        # must then fit their rate
        for source in self.sources:
            source.category = source.category or self.category
            if source.multiplier is None and source.rate is not None:
                source.multiplier = self.multiplier
        super().clean()
        validate_unique_ids(self.sources, 'sources')
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

        self.sourcemap = {s.name: s for s in self.sources}
