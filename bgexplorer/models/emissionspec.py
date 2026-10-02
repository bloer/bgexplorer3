from mongoengine import (StringField, EnumField, ValidationError,
                         ObjectIdField, EmbeddedDocument,
                         EmbeddedDocumentListField,
                         )
from bson import ObjectId
from enum import Enum
from typing import Optional, Union
from .common import units, validate_unique_ids
from .isotope import (concentration_to_rate, rate_to_concentration,
                      get_isotope, compare_source_names)
from .fields import UncertainQuantityField, AttachmentsField
from .verdoc import VersionedDocument, VersionedReferenceField
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
                              f" for multiplier {self.name}")

    def accepts(self, rate: Optional[units.Quantity]) -> bool:
        """ Does `rate` have appropriate units for us? """
        try:
            self.check_units(rate)
        except ValidationError:
            return False
        return True

    def getvalue(self, component) -> Union[float, units.Quantity]:
        """ Extract the numerical value of the multipler from component """
        mult = 1
        if self is not Multiplier.none:
            # values name the Component attributes, e.g. surface_area
            mult = getattr(component, self.value)
        return mult


class SourceCategory(Enum):
    target = 'target'
    estimate = 'estimate'
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
        # rate and id may have been set after __init__ (e.g. by forms)
        if self.rate is not None:
            self.rate.m.id = self.id
        try:
            if self.multiplier is None:
                self.multiplier = Multiplier.get_multiplier(self.rate)
                # ^ will raise ValidationError if it can't be auto-determined
            else:
                self.multiplier.check_units(self.rate)
        except ValidationError as e:
            # the rate is what's wrong, so forms can show it there
            raise ValidationError(e.message, field_name='rate') from e

        if (self.multiplier is Multiplier.mass and
                self.rate.check('ppb') and
                get_isotope(self.name) is None):

            raise ValidationError("to specify rate as concentration, `name`"
                                  " must be an isotope", field_name='name')


class EmissionSpec(VersionedDocument):
    name = StringField(required=True)
    description = StringField()
    comment = StringField()
    category = EnumField(SourceCategory)
    multiplier = EnumField(
        Multiplier,
        help_text="Used for every source whose rate has suitable units, "
                  "e.g. to choose inner or outer surface; others keep their "
                  "own")
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
        return self.name or f"new {type(self).__name__}"

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

    def mark_overrides(self, edited) -> None:
        """ Generated sources whose (str) id is in `edited` were changed by
        the user, so keep their values instead of regenerating them
        """
        for source in self.sources:
            if source.generated_from and str(source.id) in edited:
                source.generated_from = None

    def clean(self):
        """ Automatically populate derived spectra from settings """
        super().clean()
        validate_unique_ids(self.sources, 'sources')
        # sources store their multiplier once cleaned, so ours wins wherever
        # the units allow it, rather than only filling in blanks
        if self.multiplier is not None:
            for source in self.sources:
                if self.multiplier.accepts(source.rate):
                    source.multiplier = self.multiplier
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

