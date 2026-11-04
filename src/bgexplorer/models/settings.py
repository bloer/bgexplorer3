from mongoengine import (Document, StringField, BooleanField,
                         EmbeddedDocument, EmbeddedDocumentListField, signals,
                         FloatField, EnumField, MapField, URLField,
                         EmbeddedDocumentField, ListField, BinaryField,
                         ValidationError, ObjectIdField, NotUniqueError)
from mongoengine.connection import get_db
from bson import ObjectId
from pint.errors import PintError
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from .fields import UnitField, QuantityField
from ..versioning import control, hooks, record
from ..versioning.verdoc import VersionedDocument
from enum import Enum
from typing import Optional
import secrets
import logging
log = logging.getLogger(__name__)

__all__ = ['RatioType', 'get_settings', 'get_application_settings']


# Every version has exactly one VersionSettings. They all have this
# original_id, so the unique index on (version_tags, original_id) keeps it so
# and a new branch shares the settings of the version it was made from until
# they are edited there.
SETTINGS_ORIGINAL_ID = ObjectId('000000000000000000000002')


def get_settings(version_tag: Optional[str] = None, create: bool = True,
                 ) -> 'VersionSettings':
    """ Get the settings for a given version (Defaults to default_tag).
    A version without any gets default settings. If the version doesn't exist,
    it is created if `create` is true and otherwise KeyError is raised
    """
    if version_tag is None:
        version_tag = VersionedDocument.get_default_tag()
    settings = VersionSettings.select_version(version_tag).first()
    if settings is not None:
        return settings
    if not control.version_exists(version_tag):
        if not create:
            raise KeyError(version_tag)
        log.warning(f"Version '{version_tag}' not found, creating it with "
                    "default settings")
        try:
            control.create_version(version_tag, record=False)
        except KeyError:
            # another process created it at the same time
            pass
    # Not saved the usual way: that is refused for a read-only version
    new = VersionSettings(version_tag=version_tag)
    new.validate()
    try:
        VersionSettings._get_collection().insert_one(new.to_mongo())
    except DuplicateKeyError:
        # another process created it at the same time
        pass
    return VersionSettings.select_version(version_tag).get()


def touch(version_tag: str, terms_only: bool = False):
    """ Mark that data in `version_tag` changed, so any in-memory cache of
    calculated results for it is invalid. Unless `terms_only`, the
    calculation token changes as well, which invalidates the cached values of
    every SourceTerm. Use `terms_only` only for changes that are reflected in
    the SourceTerms' own stamps.
    """
    record.touch(version_tag, terms_only=terms_only)


class CalcToken(Document):
    """ Replaced when something other than a SourceTerm changes how the
    SourceTerms' values are calculated, such as a HitEfficiency. There is
    one for each version, with the id of its VersionRecord. They are made
    when needed, see `get_calc_token`
    """
    id = ObjectIdField(primary_key=True)
    token = ObjectIdField(default=ObjectId)


def get_calc_token(version_tag: str) -> Optional[ObjectId]:
    """ Return a token that changes whenever something other than a
    SourceTerm changes how the SourceTerms' values are calculated, such as a
    HitEfficiency. None if the version doesn't exist
    """
    record_id = record.record_id(version_tag)
    if record_id is None:
        return None
    coll = CalcToken._get_collection()
    query = {'_id': record_id}
    try:
        doc = coll.find_one_and_update(
            query, {'$setOnInsert': {'token': ObjectId()}}, upsert=True,
            return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        doc = coll.find_one(query)
    return doc['token']


def _replace_calc_token(version_tag, record_id=None, document=None,
                        terms_only=False, **kwargs):
    if (record_id is None or terms_only
            or getattr(document, 'VERSION_TOUCH_TERMS_ONLY', False)):
        return
    for _ in range(2):
        try:
            CalcToken._get_collection().update_one(
                {'_id': record_id}, {'$set': {'token': ObjectId()}},
                upsert=True)
            return
        except DuplicateKeyError:
            # another process created it at the same time
            pass


def _delete_calc_token(sender, document, **kwargs):
    if isinstance(document, record.VersionRecord):
        CalcToken._get_collection().delete_one({'_id': document.id})


hooks.version_changed.connect(_replace_calc_token)
signals.post_delete.connect(_delete_calc_token)


# the id of a new ApplicationSettings, so processes creating it at the same
# time can't make two
APPLICATION_SETTINGS_ID = ObjectId('000000000000000000000001')


def get_application_settings() -> 'ApplicationSettings':
    # there should only ever be one
    try:
        return ApplicationSettings.objects.get()
    except ApplicationSettings.DoesNotExist:
        log.warning("No ApplicationSettings found, creating default")
        try:
            return ApplicationSettings(id=APPLICATION_SETTINGS_ID)\
                .save(force_insert=True)
        except NotUniqueError:
            return ApplicationSettings.objects.get()


def get_server_secret(name: str) -> str:
    """ The server's generated secret `name`, created the first time it's
    needed. Secrets are kept in their own collection, so they're shared by
    every process using the database, but aren't part of any settings or
    exports
    """
    coll = get_db()['server_secrets']
    query = {'_id': name}
    try:
        doc = coll.find_one_and_update(
            query, {'$setOnInsert': {'value': secrets.token_hex()}},
            upsert=True, return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        # another process created it at the same time
        doc = coll.find_one(query)
    return doc['value']


def use_server_secret(name: str, value: str) -> bool:
    """ Delete the secret `name` if it equals `value`, so a one-time token
    can be used only once. Returns whether it did
    """
    if not value:
        return False
    return get_db()['server_secrets'].delete_one(
        {'_id': name, 'value': value}).deleted_count == 1


def get_secret_key() -> str:
    """ The generated key to use when no SECRET_KEY is configured """
    return get_server_secret('secret_key')


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
            raise ValidationError("`source` and `newsource` must be different",
                                  field_name='newsource')


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
        """ Evaluate the given ROI. Like a hit efficiency's scalars, the
        result must have units that `display_unit` accepts once multiplied
        by the hit efficiency's norm. Raises ValidationError if not
        """
        result = None
        try:
            if (hist := hiteff.spectra.get(self.link_spectrum)) is not None:
                func = getattr(hist, self.mode.name)
                result = func(self.start, self.stop, self.binwidths)
        except (PintError, ValueError) as e:
            raise ValidationError(f"ROI {self.key} can't be evaluated for "
                                  f"hiteff {hiteff.id}: {e}") from e
        if result is not None and not hiteff.check_result_unit(
                result, self.display_unit):
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

    def scalar_unit(self, key: str):
        """ The unit that the scalar or ROI `key` is displayed in, or None.
        It is for the value times the hit efficiency's norm, which is the
        scalar's unit in a background rate
        """
        if (cfg := self.display_scalars.get(key)) is not None:
            if cfg.display_unit is not None:
                return cfg.display_unit
        for roi in self.rois:
            if roi.key == key:
                return roi.display_unit
        return None

    def spectrum_unit(self, key: str):
        """ The unit that the spectrum `key` is displayed in, or None """
        cfg = self.display_spectra.get(key)
        return None if cfg is None else cfg.display_unit

    def update_from(self, hiteff):
        """ Give every scalar, spectrum and ROI of `hiteff` a display unit,
        taken from `hiteff` if it doesn't have one
        """
        for type_ in ('scalars', 'spectra'):
            for k, v in getattr(hiteff, type_).items():
                register = getattr(self, f'display_{type_}')
                cf = register.setdefault(k, HitEffConfig())
                if cf.display_unit is None:
                    cf.display_unit = hiteff.get_result_unit(v)
        for roi in self.rois:
            if (roi.display_unit is None
                    and hiteff.rois.get(roi.key) is not None):
                roi.display_unit = hiteff.get_result_unit(hiteff.rois[roi.key])



class VersionSettings(VersionedDocument):
    """ This class contains user-configurable settings """
    addsources = EmbeddedDocumentListField(AddSource,
                                           default=_default_auto_sources)
    hiteffdbconfig = EmbeddedDocumentField(HitEffDbConfig,
                                           default=HitEffDbConfig)
    # checks that other modules add, each called with the settings being
    # validated and raising ValidationError if they are not acceptable
    clean_hooks = []

    @classmethod
    def item_label(cls, son):
        return 'Settings'

    def clean(self):
        self.original_id = SETTINGS_ORIGINAL_ID
        super().clean()
        for check in self.clean_hooks:
            check(self)


class ApplicationSettings(Document):
    """ Holds configurable options for the application, such as user
    permissions and custom branding """
    org_name = StringField()
    org_logo = BinaryField()
    org_url = URLField()
    allow_anon_view = BooleanField(default=True)
