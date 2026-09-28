""" functions for creating, saving, merging, and deleting versions """
from .component import Component
from .emissionspec import EmissionSpec
from .hiteff import HitEfficiency
from .sourceterm import SourceTerm, CalculatedResults
from .settings import VersionSettings, get_settings, touch
from .verdoc import VersionedDocument, ReadOnlyVersionError
from typing import Optional, Dict
from enum import Enum
import datetime
import re
import logging
log = logging.getLogger(__name__)


_versioned_classes = [Component, EmissionSpec, HitEfficiency, SourceTerm]

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
                   ) -> VersionSettings:
    """ Create a new branch/tag. If `fromtag` is provided, create from that
    tag, otherwise create an empty version. Editable and description
    are passed to the VersionSettings object, which is returned
    """
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


def delete_version(version_tag: str) -> None:
    """ delete the selected tag. Read-only versions (tags) can be deleted.
    Raises KeyError if version_tag doesn't exist and ProtectedVersionError
    if it is the default version
    """
    if version_tag == VersionedDocument.get_default_tag():
        raise ProtectedVersionError(f"The default version '{version_tag}' "
                         "can't be deleted")
    _delete_version(version_tag)


def _delete_version(version_tag: str) -> None:
    """ delete_version without protecting the default version """
    verify_version(version_tag)
    log.warning(f"About to delete tag {version_tag}")
    for cls in _versioned_classes:
        cls.delete_tag(version_tag)
    VersionSettings.objects(version_tag=version_tag).delete()


class MergeMethod(Enum):
    replace_all = 'replace_all'
    keep_othertag = 'keep_othertag'
    keep_thistag = 'keep_thistag'
    keep_newest = 'keep_newest'


def merge_version(version_tag: str, onto: str, keep: bool = True,
                  method: MergeMethod = MergeMethod.keep_newest):
    """ Merge the documents from `version_tag` onto the tag `onto`. If `onto`
    does not exist, this is equivalent to create_version. If `keep` is False,
    `version_tag` is deleted after the merge.
    method:
        replace_all: completely delete the `onto` version then clone
        keep_othertag: only copy documents that don't match original_id
        keep_thistag: overwrite any documents with same original_id
        keep_newest: handle original_id conflicts based on modification time
    keep_othertag is likely to leave things in a bad state
    """
    log.info(f"Merging {version_tag} onto {onto} with method {method}")
    settings = get_settings(version_tag, create=False)
    if not version_exists(onto):
        create_version(onto, version_tag, editable=settings.editable,
                       description=settings.description)
    elif method is MergeMethod.replace_all:
        # delete the target version then create from this one
        _delete_version(onto)
        create_version(onto, version_tag, editable=settings.editable,
                       description=settings.description)
    elif method is MergeMethod.keep_othertag:
        for cls in _versioned_classes:
            existing = cls.select_version(onto).scalar('original_id')
            # TODO: can I do this in a single query and let the unique index
            # prevent the conflicts from occurring?
            cls.objects(version_tags=version_tag, original_id__nin=existing)\
                .update(bypass_version_control=True, push__version_tags=onto)
    elif method is MergeMethod.keep_thistag:
        settings.clone(onto)
        for cls in _versioned_classes:
            # the second half of this query excludes documents that already
            # belong to both versions
            tomerge = cls.objects(version_tags=version_tag,
                                  version_tags__ne=onto)
            cls.objects(version_tags=onto,
                        original_id__in=tomerge.scalar('original_id'),
                        ).delete(bypass_reverse_delete=True)
            tomerge.update(bypass_version_control=True,
                           push__version_tags=onto)
    elif method is MergeMethod.keep_newest:
        ontosettings = get_settings(onto)
        if ontosettings.modified < settings.modified:
            settings.clone(onto)
        for cls in _versioned_classes:
            # the second half of this query excludes documents that already
            # belong to both versions
            totest = cls.objects(version_tags=version_tag,
                                 version_tags__ne=onto,
                                 ).scalar('original_id', 'modified')
            todelete = []
            toupdate = []
            # TODO: is there a better way to do this than a loop?
            for original_id, modified in totest:
                othermtime = cls.objects(version_tags=onto,
                                         original_id=original_id)\
                                .scalar('modified').first()
                if othermtime is None:
                    toupdate.append(original_id)
                elif othermtime < modified:
                    todelete.append(original_id)
                    toupdate.append(original_id)
            cls.objects(version_tags=onto, original_id__in=todelete)\
                .delete(bypass_reverse_delete=True)
            cls.objects(version_tags=version_tag, original_id__in=toupdate)\
                .update(bypass_version_control=True, push__version_tags=onto)

    # remove all CalculatedResults
    sourceterms = SourceTerm.select_version(onto).scalar('id')
    CalculatedResults.objects(sources__in=sourceterms).delete()
    touch(onto)
    if not keep:
        delete_version(version_tag)
