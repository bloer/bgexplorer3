from mongoengine import (Document, DateTimeField, StringField, BooleanField,
                         EmbeddedDocument, EmbeddedDocumentListField, signals,
                         FloatField, EnumField, MapField, URLField,
                         EmbeddedDocumentField, ListField, BinaryField,
                         ValidationError)
from .fields import UnitField, QuantityField
from . import hiteff
from . import verdoc
from .common import units
from warnings import warn
from enum import Enum
from typing import Optional
import datetime
import logging
log = logging.getLogger(__name__)

__all__ = ['RatioType', 'get_settings', 'get_application_settings']


def get_settings(version_tag: Optional[str] = None) -> 'VersionSettings':
    if version_tag is None:
        version_tag = verdoc.VersionedDocument.get_default_tag()
    try:
        return VersionSettings.objects.get(version_tag=version_tag)
    except VersionSettings.DoesNotExist:
        log.warning("No VersionSettings found for requested version "
                    f"'{version_tag}', creating version with defaults")
        return VersionSettings(version_tag=version_tag).save()


def get_application_settings() -> 'ApplicationSettings':
    # there should only ever be one
    try:
        return ApplicationSettings.objects.get()
    except ApplicationSettings.DoesNotExist:
        log.warning("No ApplicationSettings found, creating default")
        return ApplicationSettings().save()


class RatioType(Enum):
    rate = 'rate'
    abundance = 'abundance'


class AddSource(EmbeddedDocument):
    """ specify additional source terms to add to a list from a single parent
    if they are not explicitly specified. For example, for U238, assume
    natural abundance for U235 and secular equilibrium for Ra226.
    Eventually we may expand this class to specify neutrons as well.
    """
    source = StringField(required=True)
    newsource = StringField(required=True)
    ratio = FloatField(default=1)
    ratiotype = EnumField(RatioType, default=RatioType.rate)
    comment = StringField()

    def clean(self):
        if self.source == self.newsource:
            self.error("`source` and `newsource` must be different")


def _default_auto_sources():
    # TODO: U235 natural abundance needs to be used rather than relative
    # rate if U238 rate is entered as a concentration
    return [AddSource(source="U238", newsource="U235",
                      ratio=0.00725, ratiotype='abundance',
                      comment="Relative natural abundance"),
            AddSource(source="U238", newsource="Ra226", ratio=1,
                      comment="Lower-chain U238 with secular equilibrium"),
            ]


class HitEffConfig(EmbeddedDocument):
    """ Configure settings for displaying and querying HitEfficiencies """
    display_name = StringField(required=False, default=None)
    display_unit = UnitField(required=False, default=None)
    description = StringField(default=None)
    link_spectrum = StringField(required=False, default=None)
    hide = BooleanField(required=False, default=False)

    meta = {'allow_inheritance': True}


class ROIType(Enum):
    average = "average"
    integrate = "integrate"


class SpectrumROI(HitEffConfig):
    """ Integrate or average a spectrum over an ROI """
    start = QuantityField(required=True)
    stop = QuantityField(required=True)
    mode = EnumField(ROIType, default=ROIType.average)
    binwidths = BooleanField(default=True)

    def __init__(self, spectrum=None, *args, **kwargs):
        if spectrum is not None:
            kwargs.setdefault('link_spectrum', spectrum)
        super().__init__(*args, **kwargs)

    @property
    def spectrum(self):
        return self.link_spectrum

    @spectrum.setter
    def spectrum(self, val):
        self.link_spectrum = val

    @property
    def label(self):
        return (f"{self.spectrum}, {self.mode.name} "
                f"{self.start} to {self.stop}")

    @property
    def title(self):
        return self.display_name or self.label

    @property
    def key(self):
        return self.title

    def evaluate(self, hiteff, store: bool = True):
        """ Evaluate the given ROI """
        result = None
        if (hist := hiteff.spectra.get(self.link_spectrum)) is not None:
            func = getattr(hist, self.mode.name)
            result = func(self.start, self.stop, self.binwidths)
        if self.display_unit is not None and result is not None:
            if not self.display_units.is_compatible_with(result):
                raise ValidationError(f"{self.key} hiteff {hiteff.id} units "
                                      f"don't match {self.display_units}")
        if store:
            hiteff.rois[self.key] = result
        return result


class HitEffDbConfig(EmbeddedDocument):
    """ Configure the HitEfficiency database """
    query_distribution = BooleanField(default=True)
    rois = EmbeddedDocumentListField(SpectrumROI)
    display_values = MapField(EmbeddedDocumentField(HitEffConfig),
                              default=dict)
    display_spectra = MapField(EmbeddedDocumentField(HitEffConfig),
                               default=dict)
    extra_columns = ListField(StringField())

    def update_from(self, hiteff):
        """Update display settings from a HitEfficiency """
        for type_ in ('values', 'spectra'):
            for k, v in getattr(hiteff, type_).items():
                register = getattr(self, f'display_{type_}')
                cf = register.setdefault(k, HitEffConfig())
                if cf.display_unit is None:
                    unit = hiteff.get_result_unit(v)
                    cf.display_unit = unit

    # TODO: need a post-save signal to make sure the rois in all hiteffs
    # are up-to-date


class VersionSettings(Document):
    """ This class contains user-configurable settings """
    version_tag = StringField(unique=True, required=True)
    modified = DateTimeField(default=datetime.datetime.now)
    editable = BooleanField(required=True, default=True)
    addsources = EmbeddedDocumentListField(AddSource,
                                           default=_default_auto_sources)
    hiteffdbconfig = EmbeddedDocumentField(HitEffDbConfig,
                                           default=HitEffDbConfig)

    def clean(self):
        # make sure we haven't set a display_unit that conflicts with
        # an already-existing hiteff
        for type_ in ('values', 'spectra'):
            register = getattr(self.hiteffdbconfig, f'display_{type_}')
            for k, v in register.items():
                if v.display_unit is None:
                    continue
                # get a list of all unique combinations of unit and norm type
                # for HitEfficiencies in the db
                unitlist = hiteff.HitEfficiency\
                    .select_version(self.version_tag)\
                    .aggregate([{'$group': {'_id': [f'{type_}.{k}.units',
                                                    'norm']}}])
                for entry in unitlist:
                    ustr, normstr = entry['_id']
                    val = 1 * units(ustr)
                    testhe = hiteff.HitEfficiency(norm=normstr)
                    if not testhe.check_result_unit(val, v.display_unit):
                        errmsg = (f"display_{type_}: {k} the unit "
                                  f"{v.display_unit} conflicts with at least "
                                  f"one HitEfficiency document, which has "
                                  f"units of {ustr}")
                        raise ValidationError(errmsg,
                                              field_name=f"display_{type_}")
        # TODO: need to also check rois display_units based on spectra

    @classmethod
    def post_save(cls, sender, document, **kwargs):
        # update the ROIs for all HitEfficiencies
        for he in hiteff.HitEfficiency.select_version(document.version_tag):
            rois = {roi.key: roi.evaluate(he, store=False)
                    for roi in document.hiteffdbconfig.rois}
            # use update rather than save or we'll get stuck in a loop
            he.update(set__rois=rois)


signals.post_save.connect(VersionSettings.post_save, sender=VersionSettings)


class ApplicationSettings(Document):
    """ Holds configurable options for the application, such as user
    permissions and custom branding """
    org_name = StringField()
    org_logo = BinaryField()
    org_url = URLField()
    allow_anon_view = BooleanField(default=True)


def post_save(sender, document, **kwargs):
    """ Update the 'modified' time for VersionSettings any time a versioned
    document is saved
    """

    try:
        VersionSettings.objects(version_tag=document.active_version)\
                       .update(modified=datetime.datetime.now)
    except AttributeError:
        # this isn't a VersionedDocument
        pass


signals.post_save.connect(post_save)
