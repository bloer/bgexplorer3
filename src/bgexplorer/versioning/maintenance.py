""" Database maintenance: statistics, consistency checks and cache control """
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Iterable
import datetime
import logging
from .record import VersionRecord
from .locks import release_lock
from .verdoc import (versioned_classes, parse_staging_tag, STAGING_PREFIX,
                     staged_edit_is_stale, EDIT_GRACE)
from . import control as vc
from .history import EventAction, log_event
log = logging.getLogger(__name__)


def collection_size(doc_cls) -> Dict[str, int]:
    """ Document count and data size in bytes of a collection """
    coll = doc_cls._get_collection()
    try:
        stats = coll.database.command('collStats', coll.name)
        size = stats.get('size', 0)
    except Exception:  # not supported by every server
        size = None
    return dict(count=coll.estimated_document_count(), size=size)


def database_stats() -> dict:
    """ Per-version document counts and totals per collection """
    versions = {v.version_tag: dict(editable=v.editable,
                                    counts=vc.version_summary(v.version_tag))
                for v in vc.list_versions()}
    collections = {cls.__name__: collection_size(cls)
                   for cls in versioned_classes()}
    return dict(versions=versions, collections=collections)


@dataclass
class Orphans:
    """ Inconsistent documents found by `find_orphans` """
    # class name: ids of documents with no version tags
    untagged: Dict[str, List] = field(default_factory=dict)
    # version: {class name: ids} of copies that refer to items missing from
    # the version, see `VersionedDocument.find_dangling`
    dangling: Dict[str, Dict[str, List]] = field(default_factory=dict)
    # tags used by documents that have no VersionRecord
    unknown_tags: List[str] = field(default_factory=list)
    # class name: ids of copies left by an edit that didn't finish
    interrupted: Dict[str, List] = field(default_factory=dict)

    def __bool__(self):
        return bool(self.untagged or self.dangling or self.unknown_tags
                    or self.interrupted)


def _interrupted_copies(coll, grace: datetime.timedelta):
    """ (id, original_id, version, tags) of the staged copies in `coll` that
    were made more than `grace` ago, see `VersionedQuerySet.prepare_edit`
    """
    for doc in coll.find({'version_tags': {'$regex': '^' + STAGING_PREFIX}},
                         {'original_id': 1, 'version_tags': 1}):
        if not staged_edit_is_stale(doc['_id'], grace):
            continue
        for tag in doc['version_tags']:
            if (version := parse_staging_tag(tag)) is not None:
                yield (doc['_id'], doc['original_id'], version,
                       doc['version_tags'])
                break


def recover_interrupted_edits(grace: Optional[datetime.timedelta] = None
                              ) -> Dict[str, int]:
    """ Finish or undo the edits that stopped between making a copy of a
    shared document and giving it the version: a copy whose item is no longer
    in its version takes its place, and any other is discarded. Edits that
    started less than `grace` ago (`EDIT_GRACE` by default) are left alone.
    Returns the number `restored` and `discarded`
    """
    grace = EDIT_GRACE if grace is None else grace
    known = set(VersionRecord.objects.distinct('version_tag'))
    result = dict(restored=0, discarded=0)
    for cls in versioned_classes():
        coll = cls._get_collection()
        for _id, original_id, version, tags in list(
                _interrupted_copies(coll, grace)):
            has_item = version in known and coll.count_documents(
                {'original_id': original_id, 'version_tags': version,
                 '_id': {'$ne': _id}}, limit=1)
            if version in known and not has_item:
                done = coll.update_one({'_id': _id, 'version_tags': tags},
                                       {'$set': {'version_tags': [version]}})
                result['restored'] += done.modified_count
            else:
                done = coll.delete_one({'_id': _id, 'version_tags': tags})
                result['discarded'] += done.deleted_count
    return result


def find_orphans() -> Orphans:
    """ Find documents that no version can reach or that reference missing
    documents
    """
    result = Orphans()
    for cls in versioned_classes():
        coll = cls._get_collection()
        ids = coll.distinct('_id', {'version_tags': {'$in': [None, []]}})
        if ids:
            result.untagged[cls.__name__] = ids
        if copies := [c[0] for c in _interrupted_copies(coll, EDIT_GRACE)]:
            result.interrupted[cls.__name__] = copies

    known = set(VersionRecord.objects.distinct('version_tag'))
    used = set()
    for cls in versioned_classes():
        used.update(cls._get_collection().distinct('version_tags'))
    # an empty array counts as null for distinct
    result.unknown_tags = sorted(
        t for t in used - known - {None} if not t.startswith(STAGING_PREFIX))

    for version in sorted(known):
        for cls in versioned_classes():
            if bad := cls.find_dangling(version):
                result.dangling.setdefault(version, {})[cls.__name__] = bad
    return result


def delete_orphans(orphans: Orphans = None) -> int:
    """ Remove the documents found by `find_orphans`: untagged documents are
    deleted, unknown tags are removed from all documents, dangling copies are
    removed from their version, and interrupted edits are
    recovered. Returns the number of documents changed or deleted
    """
    orphans = orphans if orphans is not None else find_orphans()
    changed = 0
    if orphans.interrupted:
        done = recover_interrupted_edits()
        changed += done['restored'] + done['discarded']
    classes = {cls.__name__: cls for cls in versioned_classes()}
    for version, found in orphans.dangling.items():
        for name, ids in found.items():
            changed += classes[name]._get_collection().update_many(
                {'_id': {'$in': ids}}, {'$pull': {'version_tags': version}}
                ).modified_count
    for cls in versioned_classes():
        coll = cls._get_collection()
        if orphans.unknown_tags:
            changed += coll.update_many(
                {'version_tags': {'$in': orphans.unknown_tags}},
                {'$pull': {'version_tags': {'$in': orphans.unknown_tags}}}
                ).modified_count
        # anything left without tags, including what was just untagged
        changed += coll.delete_many(
            {'version_tags': {'$in': [None, []]}}).deleted_count
    return changed


@dataclass
class LockInfo:
    """ A lock on a version, see `find_locks` """
    version: str
    reason: Optional[str]
    user: Optional[str]
    since: Optional[datetime.datetime]
    expires: Optional[datetime.datetime]
    token: object
    # it has run out and no longer blocks anything
    expired: bool


def find_locks(versions: Optional[Iterable[str]] = None) -> List[LockInfo]:
    """ The locks on versions, including expired ones that are still in the
    database, ordered by version
    """
    query = VersionRecord.objects(lock__ne=None).order_by('version_tag')
    if versions is not None:
        query = query(version_tag__in=list(versions))
    return [LockInfo(version=v.version_tag, reason=v.lock.reason,
                     user=v.lock.user, since=v.lock.since,
                     expires=v.lock.expires, token=v.lock.token,
                     expired=v.lock.expired)
            for v in query]


def clear_locks(versions: Optional[Iterable[str]] = None,
                expired_only: bool = False) -> List[str]:
    """ Remove the locks from `versions`, or from all versions, or only the
    expired ones if `expired_only`. Use it for locks that were left behind by
    an operation that didn't finish, never for one that is still running.
    Returns the versions that were unlocked
    """
    cleared = []
    for info in find_locks(versions):
        if expired_only and not info.expired:
            continue
        # by token, so a lock taken since it was listed isn't removed
        if release_lock(info.version, info.token):
            log_event(EventAction.clear_lock, info.version)
            cleared.append(info.version)
    return cleared
