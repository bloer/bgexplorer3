""" Locking versions while a long operation, such as a merge, changes them.

A lock is a lease that expires unless it is refreshed, so one left behind by a
crashed process frees itself. Only the context that took a lock may write to
the version, see `hold_locks`. """
from mongoengine import Q
from bson import ObjectId
from .verdoc import (set_write_guard, ReadOnlyVersionError, VersionLockedError,
                     LockLostError)
from .record import VersionRecord, VersionLock, now
from typing import Optional, Iterable, Iterator, Mapping
import contextlib
import contextvars
import datetime
import threading
import logging
log = logging.getLogger(__name__)

# How long a lock lasts without being refreshed. The holder of a lock
# refreshes it regularly, so one left behind by a crashed process or worker
# frees itself after this long.
LOCK_TTL = datetime.timedelta(minutes=5)

# {version_tag: token} of the locks held by the current thread or task, which
# may write to those versions, see `hold_locks`. Never modified in place.
held_locks: contextvars.ContextVar[Mapping[str, ObjectId]] = \
    contextvars.ContextVar('held_version_locks', default={})


def _live(lock: Optional[Mapping]) -> bool:
    """ Whether a stored lock is in force: it exists and hasn't expired. A
    lock without an expiry time never expires """
    if not lock:
        return False
    expires = lock.get('expires')
    return expires is None or expires > now()


def new_lock(reason: str, user: Optional[str] = None,
             ttl: Optional[datetime.timedelta] = LOCK_TTL) -> 'VersionLock':
    """ A lock that expires `ttl` from now, or never if `ttl` is None """
    return VersionLock(token=ObjectId(), reason=reason, user=user,
                       expires=None if ttl is None else now() + ttl)


def _lock_error(version_tag: str, lock: Mapping) -> VersionLockedError:
    reason = lock.get('reason') or 'another operation'
    return VersionLockedError(f"Version '{version_tag}' is locked by "
                                     f"{reason}, try again later")


def check_unlocked(version_tag: Optional[str]) -> None:
    """ Raise VersionLockedError if `version_tag` is locked by an operation
    that isn't running in this context. This only reports the lock at the
    moment of the check, use `hold_locks` to guarantee it stays unlocked
    """
    if version_tag is None or version_tag in held_locks.get():
        return
    doc = VersionRecord.objects(version_tag=version_tag)\
        .only('lock').as_pymongo().first()
    if doc is not None and _live(doc.get('lock')):
        raise _lock_error(version_tag, doc['lock'])


def _enforce_writable(version_tag: str) -> None:
    doc = VersionRecord.objects(version_tag=version_tag)\
        .only('editable', 'lock').as_pymongo().first()
    if doc is None:
        return
    if doc.get('editable', True) is False:
        raise ReadOnlyVersionError(
            f"Version '{version_tag}' is read-only")
    lock = doc.get('lock')
    if (held := held_locks.get().get(version_tag)) is not None:
        # the holder writes only while its own lock is still there; one
        # that expired but wasn't taken over still counts
        if not lock or lock.get('token') != held:
            raise LockLostError(
                f"The lock on version '{version_tag}' was lost, the "
                "operation can't continue")
    elif _live(lock):
        raise _lock_error(version_tag, lock)


set_write_guard(_enforce_writable)


def acquire_lock(version_tag: str, reason: str, user: Optional[str] = None,
                 ttl: Optional[datetime.timedelta] = LOCK_TTL) -> ObjectId:
    """ Lock `version_tag` and return the lock's token. The lock expires
    after `ttl` unless refreshed with `refresh_lock`; with None it never
    does. Raises VersionLockedError if it is already locked and KeyError if
    it doesn't exist
    """
    free = Q(lock=None) | Q(lock__expires__lte=now())
    for _ in range(3):
        lock = new_lock(reason, user, ttl)
        if VersionRecord.objects(Q(version_tag=version_tag) & free)\
                          .update_one(set__lock=lock):
            return lock.token
        doc = VersionRecord.objects(version_tag=version_tag)\
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
    return bool(VersionRecord.objects(
        version_tag=version_tag, lock__token=token
        ).update_one(set__lock__expires=now() + ttl))


def release_lock(version_tag: str, token: Optional[ObjectId] = None
                 ) -> bool:
    """ Remove the lock from `version_tag`. If `token` is given, only remove
    it if it matches. Returns whether a lock was removed
    """
    query = dict(version_tag=version_tag, lock__ne=None)
    if token is not None:
        query['lock__token'] = token
    return bool(VersionRecord.objects(**query).update_one(set__lock=None))


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
    VersionRecord.objects(version_tag=version_tag, lock__token=token)\
        .update_one(set__lock__token=ObjectId(), set__lock__expires=None,
                    set__lock__reason=reason)
