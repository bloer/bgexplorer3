""" functions for creating, saving, merging, and deleting versions """
from . import hooks
from .verdoc import (VersionedDocument, versioned_classes, primary_classes,
                     check_writable, strip_identity, current_user_name)
from .record import VersionRecord, get_record, touch
from .locks import (hold_locks, holding, check_unlocked, new_lock,
                    abandon_lock, held_locks)
from .diff import find_refs, raw_content, diff_versions, ItemVersions, \
    VersionComparison
from .history import EventAction, log_event
from bson import ObjectId
from mongoengine import NotUniqueError, signals
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field
from enum import Enum
import datetime
import hashlib
import json
import re
import logging
log = logging.getLogger(__name__)


class ProtectedVersionError(PermissionError):
    """ Raised when trying to delete the default version """


# version names appear as a single URL path segment
VERSION_NAME_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')


def validate_version_name(version_tag: str) -> None:
    """ Raise ValueError if `version_tag` can't be used as a version name """
    if not isinstance(version_tag, str) or not version_tag:
        raise ValueError("Version name must not be empty")
    if not VERSION_NAME_PATTERN.fullmatch(version_tag):
        raise ValueError("Version name must start with a letter or digit and "
                         "contain only letters, digits, '.', '_' and '-'")


def list_versions():
    """ Get all versions' records, ordered by name """
    return VersionRecord.objects.order_by('version_tag')


def version_summary(version_tag: str) -> Dict[str, Dict[str, int]]:
    """ Count the documents in `version_tag` for each versioned class.
    'total' is the number of documents in the version, and 'unique' the
    number that belong only to this version, which would be removed from the
    database if the version were deleted
    """
    verify_version(version_tag)
    return {cls.__name__: {'total': cls.objects(version_tags=version_tag)
                           .count(),
                           'unique': cls.objects(version_tags=[version_tag])
                           .count()}
            for cls in versioned_classes()}


def version_exists(version_tag: str) -> bool:
    """ test whether version_tag exists in the DB """
    return bool(VersionRecord.objects(version_tag=version_tag).count())


def verify_version(version_tag: str, want_exists: bool = True) -> None:
    """ Check whether the requested version exists. Raise KeyError if:
    version does not exist and `want_exists` is True or
    version does exist and `want_exists` is False
    """
    exists = version_exists(version_tag)
    if exists and not want_exists:
        raise KeyError(f"Version/tag {version_tag} already exists in the DB")
    elif not exists and want_exists:
        raise KeyError(f"Version/tag {version_tag} does not exist in the DB")


def create_version(version_tag: str, fromtag: Optional[str] = None,
                   editable: bool = True, description: Optional[str] = None,
                   record: bool = True) -> VersionRecord:
    """ Create a new branch/tag. If `fromtag` is provided, create from that
    tag, otherwise create an empty version. Editable and description
    are passed to the VersionRecord, which is returned. If `record`
    is True, the creation is logged as a VersionEvent
    """
    created = _create_version(version_tag, fromtag, editable, description)
    if record:
        log_event(EventAction.create_branch if editable
                  else EventAction.create_tag, version_tag, fromtag)
    return created


def _create_version(version_tag: str, fromtag: Optional[str],
                    editable: bool, description: Optional[str],
                    ) -> VersionRecord:
    """ create_version without logging an event """
    validate_version_name(version_tag)
    # make sure such a version doesn't already exist
    verify_version(version_tag, want_exists=False)
    if not fromtag:
        # this is the easy case, all we have to do is create a record
        log.info(f"Creating new empty version {version_tag}")
        try:
            return VersionRecord(version_tag=version_tag, editable=editable,
                                 description=description).save()
        except NotUniqueError as e:
            raise KeyError(f"Version/tag {version_tag} already exists in the "
                           "DB") from e

    # if we get here, we are constructing from previous version. The source
    # can't change while it is copied, and the new version is locked from the
    # start, until it is complete
    log.info(f"Creating new version {version_tag} from {fromtag}")
    user = current_user_name()
    with hold_locks([fromtag], f"creating '{version_tag}' from it", user):
        if description is None:
            description = get_record(fromtag).description
        lock = new_lock("being created", user)
        try:
            created = VersionRecord(version_tag=version_tag,
                                    editable=editable,
                                    description=description,
                                    lock=lock).save()
        except NotUniqueError as e:
            raise KeyError(f"Version/tag {version_tag} already exists in the "
                           "DB") from e
        with holding({version_tag: lock.token}):
            try:
                for cls in versioned_classes():
                    cls.create_tag(version_tag, fromtag)
            except BaseException:
                log.exception(f"Creating '{version_tag}' failed, removing it")
                purge_version(version_tag)
                raise
    created.reload()
    return created


def create_tag(version_tag: str, fromtag: Optional[str] = None,
               description: Optional[str] = None) -> VersionRecord:
    """ Alias for create_version with editable False """
    return create_version(version_tag, fromtag, False, description)


def create_branch(version_tag: str, fromtag: Optional[str] = None,
                  description: Optional[str] = None) -> VersionRecord:
    """ Alias for create_version with editable True """
    return create_version(version_tag, fromtag, True, description)


def delete_version(version_tag: str, allow_tags: bool = True) -> None:
    """ delete the selected tag. Read-only versions (tags) can only be deleted
    if `allow_tags` is True. Raises KeyError if version_tag doesn't exist and
    ProtectedVersionError if it is the default version or a disallowed tag
    """
    if version_tag == VersionedDocument.get_default_tag():
        raise ProtectedVersionError(f"The default version '{version_tag}' "
                         "can't be deleted")
    if not allow_tags:
        verify_version(version_tag)
        if not get_record(version_tag).editable:
            raise ProtectedVersionError(f"'{version_tag}' is a tag; tags "
                                        "can't be deleted")
    with hold_locks([version_tag], "being deleted", current_user_name()):
        purge_version(version_tag)
    log_event(EventAction.delete, version_tag)


def purge_version(version_tag: str) -> None:
    """ Remove all of a version's data, without the checks, lock and event of
    `delete_version`, so the caller must have made sure that is safe
    """
    verify_version(version_tag)
    log.warning(f"About to delete tag {version_tag}")
    # The whole version goes away, so references within it (and the signals
    # that maintain them) don't matter; work directly on the collections
    # without loading any documents.
    for cls in versioned_classes():
        coll = cls._get_collection()
        # documents used only by this version (exact array match)
        coll.delete_many({'version_tags': [version_tag]})
        # the rest are shared with other versions
        coll.update_many({'version_tags': version_tag},
                         {'$pull': {'version_tags': version_tag}})
    VersionRecord.objects(version_tag=version_tag).delete()


class VersionControlError(ValueError):
    """ Raised when an operation between versions can't be done safely.
    `problems` lists the reasons
    """
    def __init__(self, message: str, problems: Optional[List[str]] = None):
        super().__init__(message)
        self.problems = problems or []


def _adopt_copies(cls, version_tag: str, copy_ids) -> None:
    """ Make the copies with `copy_ids` (from other versions) the copies of
    their items in `version_tag`, replacing any it had. This bypasses all
    checks and signals
    """
    copy_ids = list(copy_ids)
    if not copy_ids:
        return
    coll = cls._get_collection()
    original_ids = coll.distinct('original_id', {'_id': {'$in': copy_ids}})
    # pull before pushing: each version may only have one copy of an item
    replaced = coll.distinct('_id', {'version_tags': version_tag,
                                     'original_id': {'$in': original_ids},
                                     '_id': {'$nin': copy_ids}})
    coll.update_many({'_id': {'$in': replaced}},
                     {'$pull': {'version_tags': version_tag}})
    coll.delete_many({'_id': {'$in': replaced}, 'version_tags': {'$size': 0}})
    coll.update_many({'_id': {'$in': copy_ids},
                      'version_tags': {'$ne': version_tag}},
                     {'$push': {'version_tags': version_tag}})


def missing_refs(ids, version_tag: str) -> Set[ObjectId]:
    """ The items among `ids` that aren't in `version_tag` """
    missing = set(ids)
    for cls in primary_classes():
        if not missing:
            break
        missing -= set(cls._get_collection().distinct(
            'original_id', {'version_tags': version_tag,
                            'original_id': {'$in': list(missing)}}))
    return missing


def import_document(cls, original_id, from_tag: str, to_tag: str
                    ) -> VersionedDocument:
    """ Make the copy of item `original_id` in `from_tag` also the copy in
    `to_tag`, replacing any copy `to_tag` had. Only this document is
    imported: everything it refers to must already be in `to_tag`. Returns
    the document in `to_tag`. Raises VersionControlError if it can't be
    imported, and ReadOnlyVersionError if `to_tag` can't be changed
    """
    original_id = ObjectId(original_id)
    verify_version(from_tag)
    verify_version(to_tag)
    if from_tag == to_tag:
        raise VersionControlError("Can't import a document into its own "
                                  "version")
    check_writable(to_tag)
    with hold_locks([to_tag, from_tag], f"an import from '{from_tag}' into "
                    f"'{to_tag}'", current_user_name()):
        source = cls.select_version(from_tag)(original_id=original_id)\
            .first()
        if source is None:
            raise VersionControlError(f"No {cls.__name__} {original_id} in "
                                      f"'{from_tag}'")
        refs = set(find_refs(raw_content(source))) - {original_id}
        if missing := missing_refs(refs, to_tag):
            raise VersionControlError(
                f"{getattr(source, 'name', None) or original_id} refers to "
                f"documents that "
                f"aren't in '{to_tag}'; import those first",
                [str(ref) for ref in sorted(missing)])
        _adopt_copies(type(source), to_tag, [source.id])
        doc = cls.select_version(to_tag).get(original_id=original_id)
        # lets receivers update what is derived from it
        signals.post_save.send(type(doc), document=doc, created=False)
    log_event(EventAction.import_document, to_tag, from_tag,
              message=f"{type(doc).__name__} "
                      f"{getattr(doc, 'name', None) or original_id}",
              cls=cls.__name__, original_id=str(original_id))
    return doc


class MergeError(VersionControlError):
    """ Raised when a merge can't be done safely """


class MergeRule(Enum):
    """ Which copy a merge keeps for items that differ between versions """
    source = 'source'
    target = 'target'
    newest = 'newest'


@dataclass
class ClassMergePlan:
    """ What a merge does with the items of one class. Copies of `add` and
    `replace` items are taken from the source; `keep` items differ but keep
    the target's copy, and `target_only` items are only in the target
    """
    cls: type
    add: List[ItemVersions] = field(default_factory=list)
    replace: List[ItemVersions] = field(default_factory=list)
    keep: List[ItemVersions] = field(default_factory=list)
    target_only: List[ItemVersions] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.cls.__name__

    @property
    def adopted(self) -> List[ItemVersions]:
        """ The items whose source copies the target will use """
        return self.add + self.replace


@dataclass
class MergePlan:
    """ What merging `source` into `target` with `rule` would do. The merge
    can't be done if there are `problems`. `fingerprint` identifies the
    state of both versions the plan was made from
    """
    source: str
    target: str
    rule: MergeRule
    classes: List[ClassMergePlan]
    fingerprint: str
    problems: List[str] = field(default_factory=list)
    # set once the merge is done: what receivers of `hooks.version_merged`
    # want to point out about the merged version
    warnings: List[str] = field(default_factory=list)

    @property
    def changes(self) -> bool:
        return any(c.adopted for c in self.classes)

    def counts(self) -> Dict[str, Dict[str, int]]:
        return {c.name: {'add': len(c.add), 'replace': len(c.replace),
                         'keep': len(c.keep)} for c in self.classes}


def _fingerprint(comparison: VersionComparison) -> str:
    """ A hash that changes whenever either compared version changes """
    state = [[(str(i.original_id), str(i.left_id), str(i.left_modified),
               str(i.right_id), str(i.right_modified))
              for i in (c.only_left + c.only_right + c.differ + c.equal
                        + c.same)]
             for c in comparison.classes]
    return hashlib.sha1(json.dumps(state, sort_keys=True, default=str)
                        .encode()).hexdigest()


def _source_wins(item: ItemVersions, rule: MergeRule) -> bool:
    """ Whether the source copy (right) of a differing item is merged """
    if rule is MergeRule.newest:
        # ties keep the target's copy
        return item.newer == 'right'
    return rule is MergeRule.source


def plan_merge(source: str, target: str,
               rule: MergeRule = MergeRule.newest) -> MergePlan:
    """ Work out what merging `source` into `target` would do, without
    changing anything. Items only in one version are kept, so a merge never
    removes anything from `target`. Items that differ are taken from the
    source or kept according to `rule`
    """
    rule = MergeRule(rule)
    verify_version(source)
    verify_version(target)
    if source == target:
        raise MergeError("Can't merge a version into itself")
    problems = []
    for tag, check in ((target, check_writable), (source, check_unlocked)):
        try:
            check(tag)
        except PermissionError as e:
            problems.append(str(e))
    comparison = diff_versions(target, source, classes=primary_classes())
    classes = []
    for result in comparison.classes:
        plan = ClassMergePlan(result.cls, add=result.only_right,
                              target_only=result.only_left)
        for item in result.differ:
            (plan.replace if _source_wins(item, rule) else plan.keep)\
                .append(item)
        classes.append(plan)
    result = MergePlan(source, target, rule, classes,
                       _fingerprint(comparison), problems)
    # everything the merged documents refer to must be in the target
    adopted = {c.cls: [i.right_id for i in c.adopted] for c in classes}
    merged_ids = {i.original_id for c in classes for i in c.adopted}
    refs = set()
    for cls, ids in adopted.items():
        bulky = {key: 0 for key in cls.VERSION_BULKY_FIELDS}
        for son in cls._get_collection().find({'_id': {'$in': ids}},
                                              bulky or None):
            refs.update(find_refs(strip_identity(son)))
    if missing := missing_refs(refs - merged_ids, target):
        problems.append(f"Merged documents refer to {len(missing)} items "
                        f"that are in neither version: "
                        + ', '.join(sorted(map(str, missing))))
    return result


def _backup_name(target: str) -> str:
    base = (f"{target}.premerge-"
            f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}")
    name, n = base, 1
    while version_exists(name):
        n += 1
        name = f"{base}-{n}"
    return name


def _restore(target: str, backup: str) -> None:
    """ Make `target` contain exactly the documents of `backup` again,
    keeping `target`'s own record and lock
    """
    for cls in versioned_classes():
        coll = cls._get_collection()
        ids = coll.distinct('_id', {'version_tags': target})
        coll.update_many({'_id': {'$in': ids}},
                         {'$pull': {'version_tags': target}})
        coll.delete_many({'_id': {'$in': ids}, 'version_tags': {'$size': 0}})
        coll.update_many({'version_tags': backup},
                         {'$push': {'version_tags': target}})
    touch(target)


def merge_version(source: str, target: str,
                  rule: MergeRule = MergeRule.newest,
                  fingerprint: Optional[str] = None) -> MergePlan:
    """ Merge `source` into `target`, see `plan_merge`. If `fingerprint` is
    given, it must match the plan's, i.e. neither version changed since the
    plan was shown. Both versions are locked during the merge, and `target`
    is restored if anything goes wrong. Returns the plan that was carried
    out. Raises MergeError if the merge can't be done
    """
    rule = MergeRule(rule)
    plan = plan_merge(source, target, rule)
    if plan.problems:
        raise MergeError("Can't merge", plan.problems)
    reason = f"a merge from '{source}' into '{target}'"
    with hold_locks([target, source], reason, current_user_name()):
        # plan again: either version may have changed before we locked them
        plan = plan_merge(source, target, rule)
        if plan.problems:
            raise MergeError("Can't merge", plan.problems)
        if fingerprint is not None and plan.fingerprint != fingerprint:
            raise MergeError(f"'{source}' or '{target}' changed since the "
                             "merge was planned; review it again")
        if not plan.changes:
            return plan
        backup = _backup_name(target)
        _create_version(backup, target, False,
                        f"Backup of '{target}' before merging '{source}'")
        try:
            for cls_plan in plan.classes:
                _adopt_copies(cls_plan.cls, target,
                              [i.right_id for i in cls_plan.adopted])
            # receivers update what is derived from the merged documents and
            # can check them together; they raise to undo the merge
            plan.warnings = hooks.send_collecting_warnings(
                hooks.version_merged, target, source=source, plan=plan)
        except Exception as e:
            log.exception(f"Merge of '{source}' into '{target}' failed, "
                          "restoring it")
            try:
                _restore(target, backup)
            except Exception:
                log.exception(f"Restoring '{target}' failed")
                log_event(EventAction.merge_rollback, target, source,
                          message=f"restoring failed, see backup {backup}",
                          backup=backup, error=str(e))
                abandon_lock(target, held_locks.get()[target],
                             f"a merge from '{source}' that failed and "
                             f"couldn't be undone, see the tag '{backup}'")
                raise MergeError(f"The merge failed, and so did restoring "
                                 f"'{target}'. Its previous state is in "
                                 f"the tag '{backup}'", [str(e)]) from e
            purge_version(backup)
            log_event(EventAction.merge_rollback, target, source,
                      message=str(e), error=str(e))
            raise MergeError(f"The merge failed, and '{target}' was "
                             f"restored: {e}", [str(e)]) from e
        purge_version(backup)
        touch(target)
    log_event(EventAction.merge, target, source, message=_merge_summary(plan),
              rule=rule.value, counts=plan.counts(),
              warnings=plan.warnings)
    return plan


def _merge_summary(plan: MergePlan) -> str:
    """ e.g. 'newest copies: 2 added, 1 replaced' """
    added = sum(len(c.add) for c in plan.classes)
    replaced = sum(len(c.replace) for c in plan.classes)
    return f"{plan.rule.value} copies: {added} added, {replaced} replaced"
