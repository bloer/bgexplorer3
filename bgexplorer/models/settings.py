from mongoengine import (Document, DateTimeField, StringField, BooleanField,
                         EmbeddedDocument, EmbeddedDocumentListField, signals,
                         FloatField, EnumField, MapField, URLField,
                         EmbeddedDocumentField, ListField, BinaryField,
                         ValidationError, ObjectIdField)
from bson import ObjectId
from .fields import UnitField, QuantityField
from . import hiteff
from . import verdoc
from .common import units
from enum import Enum
from typing import Optional
import datetime
import logging
log = logging.getLogger(__name__)

__all__ = ['RatioType', 'get_settings', 'get_application_settings']


def get_settings(version_tag: Optional[str] = None, create: bool = True,
                 ) -> 'VersionSettings':
    """ Get the settings for a given version (Defaults to default_tag).
    If it doesn't exist and create is true, create a default one
    if it doesn't exist and create is false, raise a KeyError
    """
    if version_tag is None:
        version_tag = verdoc.VersionedDocument.get_default_tag()
    try:
        return VersionSettings.objects.get(version_tag=version_tag)
    except VersionSettings.DoesNotExist as e:
        log.warning("No VersionSettings found for requested version "
                    f"'{version_tag}',")
        if create:
            log.warning("creating version with defaults")
            return VersionSettings(version_tag=version_tag).save()
        raise KeyError(version_tag) from e


def touch(version_tag: str):
    """ Mark that data in `version_tag` changed, so any in-memory cache of
    calculated results for it is invalid
    """
    VersionSettings.objects(version_tag=version_tag).update(
        set__modified=datetime.datetime.now(), set__cache_token=ObjectId())


def get_cache_token(version_tag: str) -> Optional[ObjectId]:
    """ Return a token that changes whenever data in `version_tag` changes,
    or None if the version has no settings
    """
    # read the raw value: mongoengine would fill in a new default for
    # settings saved before cache_token existed
    doc = VersionSettings.objects(version_tag=version_tag)\
                         .only('cache_token').as_pymongo().first()
    if doc is None:
        return None
    if doc.get('cache_token') is None:
        touch(version_tag)
        return get_cache_token(version_tag)
    return doc['cache_token']


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
            if not self.display_unit.is_compatible_with(result):
                raise ValidationError(f"{self.key} hiteff {hiteff.id} units "
                                      f"don't match {self.display_unit}")
        if store:
            hiteff.rois[self.key] = result
        return result


class HitEffDbConfig(EmbeddedDocument):
    """ Configure the HitEfficiency database """
    rois = EmbeddedDocumentListField(SpectrumROI)
    display_scalars = MapField(EmbeddedDocumentField(HitEffConfig),
                              default=dict)
    display_spectra = MapField(EmbeddedDocumentField(HitEffConfig),
                               default=dict)
    extra_columns = ListField(StringField())

    def update_from(self, hiteff):
        """Update display settings from a HitEfficiency """
        for type_ in ('scalars', 'spectra'):
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
    description = StringField()
    modified = DateTimeField(default=datetime.datetime.now)
    # replaced whenever any data in this version changes, see `touch`
    cache_token = ObjectIdField(default=ObjectId)
    editable = BooleanField(required=True, default=True)
    addsources = EmbeddedDocumentListField(AddSource,
                                           default=_default_auto_sources)
    hiteffdbconfig = EmbeddedDocumentField(HitEffDbConfig,
                                           default=HitEffDbConfig)
    meta = {
        'indexes': ['modified'],
        'ordering': ['-modified'],
    }

    def clone(self, newtag: str) -> 'VersionSettings':
        """ Copy ourselves to a new tag, overwriting any existing settings
        for that tag """
        VersionSettings.objects(id=self.id).aggregate([
            {'$unset': '_id'},
            {'$set': {'version_tag': newtag,
                      'modified': datetime.datetime.now(),
                      'cache_token': ObjectId()}},
            {'$merge': {'into': VersionSettings.objects._collection.name,
                        'on': 'version_tag',
                        'whenMatched': 'replace'}},
            ])
        return VersionSettings.objects.get(version_tag=newtag)

    def clean(self):
        # settings of a read-only version can't be changed, including
        # making it editable again
        if self.id is not None:
            verdoc.check_writable(VersionSettings.objects(id=self.id)
                                  .scalar('version_tag').first())
        # make sure we haven't set a display_unit that conflicts with
        # an already-existing hiteff
        for type_ in ('scalars', 'spectra'):
            register = getattr(self.hiteffdbconfig, f'display_{type_}')
            for k, v in register.items():
                if v.display_unit is None:
                    continue
                # get a list of all unique combinations of unit and norm type
                # for HitEfficiencies in the db
                unitlist = hiteff.HitEfficiency\
                    .select_version(self.version_tag)\
                    .aggregate([{'$group': {'_id': [f'${type_}.{k}.units',
                                                    '$norm']}}])
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
        touch(document.version_tag)


signals.post_save.connect(VersionSettings.post_save, sender=VersionSettings)


class ApplicationSettings(Document):
    """ Holds configurable options for the application, such as user
    permissions and custom branding """
    org_name = StringField()
    org_logo = BinaryField()
    org_url = URLField()
    allow_anon_view = BooleanField(default=True)


def post_save(sender, document, **kwargs):
    """ Update the 'modified' time and cache token for VersionSettings any
    time a versioned document is saved or deleted
    """
    version_tag = getattr(document, 'active_version', None)
    if version_tag is not None:
        touch(version_tag)


signals.post_save.connect(post_save)
signals.post_delete.connect(post_save)
