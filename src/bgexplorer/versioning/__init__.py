"""Copy-on-write versioning of MongoEngine documents

Documents of classes derived from `VersionedDocument` belong to one or more
named versions, like commits on a git branch; editing a document shared with
other versions makes a copy. `VersionRecord` tracks the versions themselves,
`control` creates, deletes and merges them, `archive` writes them to files and
`hooks` lets the application react to merges and imports. See
docs/versions.md.
"""
from . import hooks, maintenance  # noqa: F401
from .verdoc import (VersionedDocument, DynamicVersionedDocument,
                     VersionedReferenceField, VersionedListField,
                     VersionedEmbeddedDocumentListField,
                     ReadOnlyVersionError, VersionLockedError, LockLostError,
                     set_user_provider, versioned_classes, primary_classes)
from .record import (VersionRecord, VersionLock, get_record, get_cache_token,
                     touch)
from .locks import (acquire_lock, release_lock, refresh_lock, hold_locks,
                    holding, abandon_lock, check_unlocked)
from .control import (create_version, create_branch, create_tag,
                      delete_version, list_versions, version_exists,
                      import_document, plan_merge, merge_version, MergePlan,
                      MergeRule, MergeError, VersionControlError,
                      ProtectedVersionError)
from .diff import diff_versions, compare_document, DiffEntry
from .archive import export_version, import_version, VersionFileError
from .history import EventAction, VersionEvent, recent_events

__all__ = [
    'VersionedDocument', 'DynamicVersionedDocument', 'VersionedReferenceField',
    'VersionedListField', 'VersionedEmbeddedDocumentListField',
    'ReadOnlyVersionError', 'VersionLockedError', 'LockLostError',
    'set_user_provider', 'versioned_classes', 'primary_classes',
    'VersionRecord', 'VersionLock', 'get_record', 'get_cache_token', 'touch',
    'acquire_lock', 'release_lock', 'refresh_lock', 'hold_locks', 'holding',
    'abandon_lock', 'check_unlocked',
    'create_version', 'create_branch', 'create_tag', 'delete_version',
    'list_versions', 'version_exists', 'import_document', 'plan_merge',
    'merge_version', 'MergePlan', 'MergeRule', 'MergeError',
    'VersionControlError', 'ProtectedVersionError',
    'diff_versions', 'compare_document', 'DiffEntry',
    'export_version', 'import_version', 'VersionFileError',
    'EventAction', 'VersionEvent', 'recent_events', 'hooks', 'maintenance',
]
