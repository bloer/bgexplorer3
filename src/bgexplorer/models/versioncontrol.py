""" functions for creating, saving, merging, and deleting versions """
from .component import Component, Assembly
from .emissionspec import EmissionSpec
from .hiteff import HitEfficiency
from .cosmogenic import ActivatedMaterial
from .sourceterm import SourceTerm, CalculatedResults
from .settings import VersionSettings, get_settings, touch, hold_locks
from .verdoc import (VersionedDocument, ReadOnlyVersionError, check_unlocked,
                     check_writable, strip_identity, current_user_name)
from .versiondiff import (find_refs, raw_content, diff_versions,
                          settings_content, ItemVersions, DiffEntry,
                          VersionComparison)
from .history import EventAction, log_event
from . import signals
from bson import ObjectId
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field
from enum import Enum
import datetime
import hashlib
import json
import re
import logging
log = logging.getLogger(__name__)


_versioned_classes = [Component, EmissionSpec, HitEfficiency, ActivatedMaterial,
                      SourceTerm]

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
    """ Get all versions' settings, ordered by name """
    return VersionSettings.objects.order_by('version_tag')


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
            for cls in _versioned_classes}


def version_exists(version_tag: str) -> bool:
    """ test whether version_tag exists in the DB """
    return bool(VersionSettings.objects(version_tag=version_tag).count())


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
                   record: bool = True) -> VersionSettings:
    """ Create a new branch/tag. If `fromtag` is provided, create from that
    tag, otherwise create an empty version. Editable and description
    are passed to the VersionSettings object, which is returned. If `record`
    is True, the creation is logged as a VersionEvent
    """
    settings = _create_version(version_tag, fromtag, editable, description)
    if record:
        log_event(EventAction.create_branch if editable
                  else EventAction.create_tag, version_tag, fromtag)
    return settings


def _create_version(version_tag: str, fromtag: Optional[str],
                    editable: bool, description: Optional[str],
                    ) -> VersionSettings:
    """ create_version without logging an event """
    validate_version_name(version_tag)
    # make sure such a version doesn't already exist
    verify_version(version_tag, want_exists=False)
    if not fromtag:
        # this is the easy case, all we have to do is create a settings object
        log.info(f"Creating new empty version {version_tag}")
        return VersionSettings(version_tag=version_tag, editable=editable,
                               description=description).save()

    # if we get here, we are constructing from previous version
    # clone into a new version
    log.info(f"Creating new version {version_tag} from {fromtag}")
    newsettings = get_settings(fromtag, create=False).clone(version_tag)
    # use update rather than save: settings cloned from a tag are read-only
    changes = dict(set__editable=editable,
                   set__modified=datetime.datetime.now())
    if description is not None:
        changes['set__description'] = description
    newsettings.update(**changes)
    for cls in _versioned_classes:
        cls.create_tag(version_tag, fromtag)
    newsettings.reload()
    return newsettings


def create_tag(version_tag: str, fromtag: Optional[str] = None,
               description: Optional[str] = None) -> VersionSettings:
    """ Alias for create_version with editable False """
    return create_version(version_tag, fromtag, False, description)


def create_branch(version_tag: str, fromtag: Optional[str] = None,
                  description: Optional[str] = None) -> VersionSettings:
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
        if not VersionSettings.objects.get(version_tag=version_tag).editable:
            raise ProtectedVersionError(f"'{version_tag}' is a tag; tags "
                                        "can't be deleted")
    check_unlocked(version_tag)
    _delete_version(version_tag)
    log_event(EventAction.delete, version_tag)


def _delete_version(version_tag: str) -> None:
    """ delete_version without protecting the default version """
    verify_version(version_tag)
    log.warning(f"About to delete tag {version_tag}")
    for cls in _versioned_classes:
        cls.delete_tag(version_tag)
    VersionSettings.objects(version_tag=version_tag).delete()


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


# classes that versioned documents refer to
_referenced_classes = [Component, EmissionSpec, HitEfficiency,
                       ActivatedMaterial]


def missing_refs(ids, version_tag: str) -> Set[ObjectId]:
    """ The items among `ids` that aren't in `version_tag` """
    missing = set(ids)
    for cls in _referenced_classes:
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
    with hold_locks([to_tag], f"an import from '{from_tag}'"):
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
        # update the SourceTerms that are derived from it
        signals.post_save(sender=type(doc), document=doc)
        touch(to_tag)
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
    settings: List[DiffEntry]
    replace_settings: bool
    fingerprint: str
    problems: List[str] = field(default_factory=list)

    @property
    def changes(self) -> bool:
        return self.replace_settings or any(c.adopted for c in self.classes)

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
    state.append([settings_content(comparison.left),
                  settings_content(comparison.right)])
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
    removes anything from `target`. Items that differ, and the settings,
    are taken from the source or kept according to `rule`. For settings,
    `newest` compares the times the versions were last changed
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
    comparison = diff_versions(target, source, classes=_referenced_classes)
    classes = []
    for result in comparison.classes:
        plan = ClassMergePlan(result.cls, add=result.only_right,
                              target_only=result.only_left)
        for item in result.differ:
            (plan.replace if _source_wins(item, rule) else plan.keep)\
                .append(item)
        classes.append(plan)
    if rule is MergeRule.newest:
        modified = {tag: get_settings(tag, create=False).modified
                    for tag in (source, target)}
        source_settings = modified[source] > modified[target]
    else:
        source_settings = rule is MergeRule.source
    result = MergePlan(source, target, rule, classes, comparison.settings,
                       bool(comparison.settings) and source_settings,
                       _fingerprint(comparison), problems)
    # everything the merged documents refer to must be in the target
    adopted = {c.cls: [i.right_id for i in c.adopted] for c in classes}
    merged_ids = {i.original_id for c in classes for i in c.adopted}
    refs = set()
    for cls, ids in adopted.items():
        for son in cls._get_collection().find({'_id': {'$in': ids}},
                                              {'attachments.data': 0}):
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


def _copy_settings(source: str, target: str) -> None:
    """ Replace the content of `target`'s settings with `source`'s, keeping
    what belongs to the version itself, such as its name and lock
    """
    content = settings_content(source)
    unset = {key: '' for key in settings_content(target)
             if key not in content}
    update = {'$set': content} if content else {}
    if unset:
        update['$unset'] = unset
    if update:
        VersionSettings._get_collection().update_one(
            {'version_tag': target}, update)


def _restore(target: str, backup: str) -> None:
    """ Make `target` contain exactly the documents and settings of
    `backup` again, keeping `target`'s own settings document and lock
    """
    for cls in _versioned_classes:
        coll = cls._get_collection()
        ids = coll.distinct('_id', {'version_tags': target})
        coll.update_many({'_id': {'$in': ids}},
                         {'$pull': {'version_tags': target}})
        coll.delete_many({'_id': {'$in': ids}, 'version_tags': {'$size': 0}})
        coll.update_many({'version_tags': backup},
                         {'$push': {'version_tags': target}})
    _copy_settings(backup, target)
    touch(target)


def _update_rois(version_tag: str) -> None:
    """ Evaluate the ROIs of the hit efficiencies in `version_tag` with its
    settings, and save those that changed
    """
    rois = get_settings(version_tag).hiteffdbconfig.rois
    for hiteff in HitEfficiency.select_version(version_tag):
        values = {roi.key: roi.evaluate(hiteff, store=False) for roi in rois}
        try:
            changed = values != dict(hiteff.rois)
        except Exception:
            changed = True
        if changed:
            hiteff.update(set__rois=values)


def _clear_results(version_tag: str) -> None:
    """ Delete the stored results calculated from a version's SourceTerms """
    CalculatedResults.objects(
        sources__in=list(SourceTerm.select_version(version_tag).scalar('id'))
    ).delete()


def _update_sourceterms(target: str, plan: MergePlan) -> None:
    """ Update the SourceTerms of `target` that depend on the documents a
    merge adopted, as if each had been saved there. New settings can change
    any of them, so then all are rebuilt, as they are when most components
    are affected
    """
    from .maintenance import rebuild_sourceterms
    adopted = {c.cls: [i.original_id for i in c.adopted]
               for c in plan.classes}
    components = Component.select_version(target)
    # updating each term separately costs more per term than a rebuild
    touched = components(__raw__={'$or': [
        {'original_id': {'$in': adopted.get(Component, [])}},
        {'specs': {'$in': adopted.get(EmissionSpec, [])}}]}).count()
    if plan.replace_settings or touched > components.count() / 2:
        rebuild_sourceterms(target)
        return
    docs = []
    # materials update their activation specs, specs their components, and
    # components their assemblies; hit efficiencies match the final terms
    for cls in (ActivatedMaterial, EmissionSpec, Component, HitEfficiency):
        if ids := adopted.get(cls):
            found = list(cls.select_version(target)(original_id__in=ids))
            if cls is Component:
                # leaves, then assemblies from the bottom up
                found.sort(key=lambda c: (isinstance(c, Assembly),
                                          c.hierarchy_level))
            docs.extend(found)
    log.info(f"Updating SourceTerms in '{target}' for {len(docs)} merged "
             "documents")
    for doc in docs:
        signals.post_save(sender=type(doc), document=doc)


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
    locked = [target] + ([source] if get_settings(source).editable else [])
    with hold_locks(locked, f"a merge from '{source}' into '{target}'",
                    current_user_name()):
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
            _clear_results(target)
            for cls_plan in plan.classes:
                _adopt_copies(cls_plan.cls, target,
                              [i.right_id for i in cls_plan.adopted])
            if plan.replace_settings:
                _copy_settings(source, target)
            if any(entry.path[:2] == ['hiteffdbconfig', 'rois']
                   for entry in plan.settings):
                # some hit efficiencies' ROIs were evaluated with the other
                # version's settings
                _update_rois(target)
            _update_sourceterms(target, plan)
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
                raise MergeError(f"The merge failed, and so did restoring "
                                 f"'{target}'. Its previous state is in "
                                 f"the tag '{backup}'", [str(e)]) from e
            _delete_version(backup)
            log_event(EventAction.merge_rollback, target, source,
                      message=str(e), error=str(e))
            raise MergeError(f"The merge failed, and '{target}' was "
                             f"restored: {e}", [str(e)]) from e
        _delete_version(backup)
        touch(target)
    log_event(EventAction.merge, target, source, message=_merge_summary(plan),
              rule=rule.value, counts=plan.counts(),
              settings=plan.replace_settings)
    return plan


def _merge_summary(plan: MergePlan) -> str:
    """ e.g. 'newest copies: 2 added, 1 replaced, settings replaced' """
    added = sum(len(c.add) for c in plan.classes)
    replaced = sum(len(c.replace) for c in plan.classes)
    text = f"{plan.rule.value} copies: {added} added, {replaced} replaced"
    if plan.replace_settings:
        text += ", settings replaced"
    return text
