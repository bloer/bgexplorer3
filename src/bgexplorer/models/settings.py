from mongoengine import (Q, Document, DateTimeField, StringField, BooleanField,
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
from . import verdoc
from enum import Enum
from typing import Optional, Iterable, Iterator, Mapping
import contextlib
import contextvars
import secrets
import threading
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
            try:
                return VersionSettings(version_tag=version_tag).save()
            except NotUniqueError:
                # another process created it at the same time
                return VersionSettings.objects.get(version_tag=version_tag)
        raise KeyError(version_tag) from e


def touch(version_tag: str, terms_only: bool = False):
    """ Mark that data in `version_tag` changed, so any in-memory cache of
    calculated results for it is invalid. Unless `terms_only`, the
    calculation token changes as well, which invalidates the cached values of
    every SourceTerm. Use `terms_only` only for changes that are reflected in
    the SourceTerms' own stamps.
    """
    update = dict(set__modified=datetime.datetime.now(),
                  set__cache_token=ObjectId())
    if not terms_only:
        update['set__calc_token'] = ObjectId()
    VersionSettings.objects(version_tag=version_tag).update(**update)


def _get_token(version_tag: str, field: str) -> Optional[ObjectId]:
    # read the raw value: mongoengine would fill in a new default for
    # settings saved before the token existed
    doc = VersionSettings.objects(version_tag=version_tag)\
                         .only(field).as_pymongo().first()
    if doc is None:
        return None
    if doc.get(field) is None:
        touch(version_tag)
        return _get_token(version_tag, field)
    return doc[field]


def get_cache_token(version_tag: str) -> Optional[ObjectId]:
    """ Return a token that changes whenever data in `version_tag` changes,
    or None if the version has no settings
    """
    return _get_token(version_tag, 'cache_token')


def get_calc_token(version_tag: str) -> Optional[ObjectId]:
    """ Return a token that changes whenever something other than a
    SourceTerm changes how the SourceTerms' values are calculated, such as a
    HitEfficiency. None if the version has no settings
    """
    return _get_token(version_tag, 'calc_token')


# How long a lock lasts without being refreshed. The holder of a lock
# refreshes it regularly, so one left behind by a crashed process or worker
# frees itself after this long.
LOCK_TTL = datetime.timedelta(minutes=5)

# {version_tag: token} of the locks held by the current thread or task, which
# may write to those versions, see `hold_locks`. Never modified in place.
held_locks: contextvars.ContextVar[Mapping[str, ObjectId]] = \
    contextvars.ContextVar('held_version_locks', default={})


def _now() -> datetime.datetime:
    """ The current time as MongoDB stores it: UTC without a time zone """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _live(lock: Optional[Mapping]) -> bool:
    """ Whether a stored lock is in force: it exists and hasn't expired. A
    lock without an expiry time never expires """
    if not lock:
        return False
    expires = lock.get('expires')
    return expires is None or expires > _now()


def new_lock(reason: str, user: Optional[str] = None,
             ttl: Optional[datetime.timedelta] = LOCK_TTL) -> 'VersionLock':
    """ A lock that expires `ttl` from now, or never if `ttl` is None """
    return VersionLock(token=ObjectId(), reason=reason, user=user,
                       expires=None if ttl is None else _now() + ttl)


def _lock_error(version_tag: str, lock: Mapping) -> verdoc.VersionLockedError:
    reason = lock.get('reason') or 'another operation'
    return verdoc.VersionLockedError(f"Version '{version_tag}' is locked by "
                                     f"{reason}, try again later")


def check_unlocked(version_tag: Optional[str]) -> None:
    """ Raise VersionLockedError if `version_tag` is locked by an operation
    that isn't running in this context. This only reports the lock at the
    moment of the check, use `hold_locks` to guarantee it stays unlocked
    """
    if version_tag is None or version_tag in held_locks.get():
        return
    doc = VersionSettings.objects(version_tag=version_tag)\
        .only('lock').as_pymongo().first()
    if doc is not None and _live(doc.get('lock')):
        raise _lock_error(version_tag, doc['lock'])


def _enforce_writable(version_tag: str) -> None:
    doc = VersionSettings.objects(version_tag=version_tag)\
        .only('editable', 'lock').as_pymongo().first()
    if doc is None:
        return
    if doc.get('editable', True) is False:
        raise verdoc.ReadOnlyVersionError(
            f"Version '{version_tag}' is read-only")
    lock = doc.get('lock')
    if (held := held_locks.get().get(version_tag)) is not None:
        # the holder writes only while its own lock is still there; one
        # that expired but wasn't taken over still counts
        if not lock or lock.get('token') != held:
            raise verdoc.LockLostError(
                f"The lock on version '{version_tag}' was lost, the "
                "operation can't continue")
    elif _live(lock):
        raise _lock_error(version_tag, lock)


verdoc.set_write_guard(_enforce_writable)


def acquire_lock(version_tag: str, reason: str, user: Optional[str] = None,
                 ttl: Optional[datetime.timedelta] = LOCK_TTL) -> ObjectId:
    """ Lock `version_tag` and return the lock's token. The lock expires
    after `ttl` unless refreshed with `refresh_lock`; with None it never
    does. Raises VersionLockedError if it is already locked and KeyError if
    it doesn't exist
    """
    free = Q(lock=None) | Q(lock__expires__lte=_now())
    for _ in range(3):
        lock = new_lock(reason, user, ttl)
        if VersionSettings.objects(Q(version_tag=version_tag) & free)\
                          .update_one(set__lock=lock):
            return lock.token
        doc = VersionSettings.objects(version_tag=version_tag)\
            .only('lock').as_pymongo().first()
        if doc is None:
            raise KeyError(version_tag)
        if _live(doc.get('lock')):
            raise _lock_error(version_tag, doc['lock'])
        # it was released or expired between the two queries; try again
    raise _lock_error(version_tag, {})


def refresh_lock(version_tag: str, token: ObjectId,
                 ttl: datetime.timedelta = LOCK_TTL) -> bool:
    """ Extend the lock with `token` to expire `ttl` from now. Returns False
    if `version_tag` no longer has that lock, i.e. it was cleared or taken
    over. Long operations that don't use `hold_locks` should call this
    regularly
    """
    return bool(VersionSettings.objects(
        version_tag=version_tag, lock__token=token
        ).update_one(set__lock__expires=_now() + ttl))


def release_lock(version_tag: str, token: Optional[ObjectId] = None
                 ) -> bool:
    """ Remove the lock from `version_tag`. If `token` is given, only remove
    it if it matches. Returns whether a lock was removed
    """
    query = dict(version_tag=version_tag, lock__ne=None)
    if token is not None:
        query['lock__token'] = token
    return bool(VersionSettings.objects(**query).update_one(set__lock=None))


class _Heartbeat(threading.Thread):
    """ Refreshes locks in the background until stopped """
    def __init__(self, tokens: Mapping[str, ObjectId],
                 ttl: datetime.timedelta):
        super().__init__(name='version-lock-heartbeat', daemon=True)
        self.tokens = dict(tokens)
        self.ttl = ttl
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.wait(self.ttl.total_seconds() / 3):
            for tag, token in list(self.tokens.items()):
                try:
                    if not refresh_lock(tag, token, self.ttl):
                        log.error(f"The lock on version '{tag}' was lost")
                        del self.tokens[tag]
                except Exception:
                    log.exception(f"Couldn't refresh the lock on '{tag}'")

    def stop(self) -> None:
        self._halt.set()
        self.join()


@contextlib.contextmanager
def hold_locks(version_tags: Iterable[str], reason: str,
               user: Optional[str] = None,
               ttl: Optional[datetime.timedelta] = LOCK_TTL
               ) -> Iterator[None]:
    """ Lock all of `version_tags` for the duration of the context. Writes
    to them are allowed only from within the context. Raises
    VersionLockedError, without holding any locks, if any is already locked.
    Versions that the current context already holds are left as they are.
    The locks are refreshed in the background, so they last as long as the
    context does but free themselves soon after a crash; `ttl` of None
    disables that
    """
    held = held_locks.get()
    tokens = {}
    try:
        # a fixed order, so concurrent callers can't each hold half
        for tag in sorted(set(version_tags) - set(held)):
            tokens[tag] = acquire_lock(tag, reason, user, ttl)
    except BaseException:
        for tag, token in tokens.items():
            release_lock(tag, token)
        raise
    with holding(tokens, ttl):
        yield


@contextlib.contextmanager
def holding(tokens: Mapping[str, ObjectId],
            ttl: Optional[datetime.timedelta] = LOCK_TTL) -> Iterator[None]:
    """ Allow writes to the versions locked with {version_tag: token}
    during the context, keep the locks alive, then release them
    """
    reset = held_locks.set({**held_locks.get(), **tokens})
    heartbeat = None
    if tokens and ttl is not None:
        heartbeat = _Heartbeat(tokens, ttl)
        heartbeat.start()
    try:
        yield
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        held_locks.reset(reset)
        for tag, token in tokens.items():
            release_lock(tag, token)


def abandon_lock(version_tag: str, token: ObjectId, reason: str) -> None:
    """ Turn a lock held by this context into one that nobody holds and that
    never expires, so that it stays when the context ends. For operations
    that fail and leave a version in a state that needs an administrator,
    who can clear it, see `maintenance.clear_locks`
    """
    VersionSettings.objects(version_tag=version_tag, lock__token=token)\
        .update_one(set__lock__token=ObjectId(), set__lock__expires=None,
                    set__lock__reason=reason)


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


class VersionLock(EmbeddedDocument):
    """ Marks a version as being changed by a long operation, such as a
    merge. Only the holder may write to a locked version, see `hold_locks`
    """
    token = ObjectIdField(required=True)
    reason = StringField()
    user = StringField()
    since = DateTimeField(default=datetime.datetime.now)
    # the lock is free after this time (UTC) unless refreshed, see
    # `refresh_lock`. None: it doesn't expire
    expires = DateTimeField(default=None)

    @property
    def expired(self) -> bool:
        return self.expires is not None and self.expires <= _now()


class VersionSettings(Document):
    """ This class contains user-configurable settings """
    version_tag = StringField(unique=True, required=True)
    description = StringField()
    modified = DateTimeField(default=datetime.datetime.now)
    # replaced whenever any data in this version changes, see `touch`
    cache_token = ObjectIdField(default=ObjectId)
    # replaced when SourceTerm values change for reasons other than the
    # SourceTerms' own content, see `touch`
    calc_token = ObjectIdField(default=ObjectId)
    editable = BooleanField(required=True, default=True)
    addsources = EmbeddedDocumentListField(AddSource,
                                           default=_default_auto_sources)
    hiteffdbconfig = EmbeddedDocumentField(HitEffDbConfig,
                                           default=HitEffDbConfig)
    # set while an operation such as a merge is changing this version
    lock = EmbeddedDocumentField(VersionLock, default=None)
    # checks that other modules add, each called with the settings being
    # validated and raising ValidationError if they are not acceptable
    clean_hooks = []
    meta = {
        'indexes': ['modified'],
        'ordering': ['-modified'],
    }

    @property
    def active_lock(self) -> Optional['VersionLock']:
        """ The lock on this version, unless it has expired """
        return None if self.lock is None or self.lock.expired else self.lock

    def clone(self, newtag: str, lock: Optional['VersionLock'] = None,
              overwrite: bool = True) -> 'VersionSettings':
        """ Copy ourselves to a new tag, with `lock` already on it, if given.
        Existing settings for that tag are replaced if `overwrite` is true,
        and otherwise raise KeyError
        """
        stages = [{'$unset': ['_id', 'lock']},
                  {'$set': {'version_tag': newtag,
                            'modified': datetime.datetime.now(),
                            'cache_token': ObjectId(),
                            'calc_token': ObjectId()}}]
        if lock is not None:
            stages.append({'$set': {'lock': lock.to_mongo().to_dict()}})
        stages.append(
            {'$merge': {'into': VersionSettings.objects._collection.name,
                        'on': 'version_tag',
                        'whenMatched': 'replace' if overwrite else 'fail'}})
        try:
            VersionSettings.objects(id=self.id).aggregate(stages)
        except DuplicateKeyError as e:
            raise KeyError(f"Version/tag {newtag} already exists in the DB"
                           ) from e
        return VersionSettings.objects.get(version_tag=newtag)

    def clean(self):
        # settings of a read-only version can't be changed, including
        # making it editable again
        if self.id is not None:
            verdoc.check_writable(VersionSettings.objects(id=self.id)
                                  .scalar('version_tag').first())
        for check in self.clean_hooks:
            check(self)

    @classmethod
    def post_save(cls, sender, document, **kwargs):
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
        touch(version_tag, terms_only=getattr(
            document, 'VERSION_TOUCH_TERMS_ONLY', False))


signals.post_save.connect(post_save)
signals.post_delete.connect(post_save)
