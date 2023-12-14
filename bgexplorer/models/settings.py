from mongoengine import (Document, DateTimeField, StringField, BooleanField,
                         EmbeddedDocument, EmbeddedDocumentListField,
                         FloatField, EnumField, signals, MapField,
                         EmbeddedDocumentField, ListField, BinaryField, URLField)
from .fields import UnitField, QuantityField
from warnings import warn
from enum import Enum, Flag
import datetime

__all__ = ['RatioType', 'get_settings', 'get_application_settings']


def get_settings(version_tag: str) -> 'VersionSettings':
    try:
        return VersionSettings.objects.get(version_tag=version_tag)
    except VersionSettings.DoesNotExist:
        warn(f"No VersionSettings found for requested version {version_tag}"
             ", creating version with defaults")
        return VersionSettings(version_tag=version_tag).save()


def get_application_settings() -> 'ApplicationSettings':
    # there should only ever be one
    return ApplicationSettings.objects.get()


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
    key = StringField(required=True)
    display_name = StringField(required=False, default=None)
    display_unit = UnitField(required=False, default=None)
    description = StringField(default=None)
    link_spectrum = StringField(required=False, default=None)
    hide = BooleanField(required=False, default=False)

    @property
    def title(self):
        return self.display_name or self.key


class ROIType(Enum):
    average = "average"
    integrate = "integrate"


class SpectrumROI(EmbeddedDocument):
    """ Integrate or average a spectrum over an ROI """
    label = StringField()
    spectrum = StringField(required=True)
    start = QuantityField(required=True)
    stop = QuantityField(required=True)
    mode = EnumField(ROIType, default=ROIType.average)
    binwidths = BooleanField(default=True)

    @property
    def key(self):
        return self.label or \
            f"{self.spectrum}, {self.mode.name} {self.start} to {self.stop}"

    def evaluate(self, hiteff, store: bool = True):
        """ Evaluate the given ROI """
        result = None
        if (hist := hiteff.spectra.get(self.spectrum)) is not None:
            func = getattr(hist, self.mode.name)
            result = func(self.start, self.stop, self.binwidths)
        if store:
            hiteff.values[self.key] = result
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
        for k, v in hiteff.values.items():
            unit = hiteff.get_result_unit(v)
            self.display_values.setdefault(
                k,
                HitEffConfig(key=k, display_unit=unit)
                )
        for k, v in hiteff.spectra.items():
            unit = hiteff.get_result_unit(v)
            self.display_spectra.setdefault(
                k,
                HitEffConfig(key=k, display_unit=unit)
                )


class VersionSettings(Document):
    """ This class contains user-configurable settings """
    version_tag = StringField(unique=True, required=True)
    modified = DateTimeField(default=datetime.datetime.now)
    editable = BooleanField(required=True, default=True)
    addsources = EmbeddedDocumentListField(AddSource,
                                           default=_default_auto_sources)
    hiteffdbconfig = EmbeddedDocumentField(HitEffDbConfig,
                                           default=HitEffDbConfig)




class ApplicationSettings(Document):
    """ Holds configurable options for the application, such as user
    permissions and custom branding """
    org_name = StringField()
    org_logo = BinaryField()
    org_url = URLField()
    allow_anon_view = BooleanField(default=False)


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
