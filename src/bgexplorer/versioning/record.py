""" The record of a version: that it exists, when it changed and whether it can
be edited, see `locks` for locking it """
from mongoengine import (Document, EmbeddedDocument, DateTimeField,
                         StringField, BooleanField, EmbeddedDocumentField,
                         ObjectIdField, signals)
from bson import ObjectId
from typing import Optional
import datetime
from . import hooks
from .verdoc import VersionedDocument, check_writable


def now() -> datetime.datetime:
    """ The current time as MongoDB stores it: UTC without a time zone """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


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
        return self.expires is not None and self.expires <= now()


class VersionRecord(Document):
    """ One per version. Everything else about a version is stored in
    versioned documents, which this class doesn't know about
    """
    version_tag = StringField(unique=True, required=True)
    description = StringField()
    modified = DateTimeField(default=datetime.datetime.now)
    # replaced whenever any data in this version changes, see `touch`
    cache_token = ObjectIdField(default=ObjectId)
    editable = BooleanField(required=True, default=True)
    # set while an operation such as a merge is changing this version
    lock = EmbeddedDocumentField(VersionLock, default=None)
    meta = {
        'collection': 'version_record',
        'indexes': ['modified'],
        'ordering': ['-modified'],
    }

    @property
    def active_lock(self) -> Optional[VersionLock]:
        """ The lock on this version, unless it has expired """
        return None if self.lock is None or self.lock.expired else self.lock

    def clean(self):
        # the record of a read-only version can't be changed, including
        # making it editable again
        if self.id is not None:
            check_writable(VersionRecord.objects(id=self.id)
                           .scalar('version_tag').first())


def get_record(version_tag: str) -> VersionRecord:
    """ The record of `version_tag`. Raises KeyError if it doesn't exist """
    record = VersionRecord.objects(version_tag=version_tag).first()
    if record is None:
        raise KeyError(f"Version/tag {version_tag} does not exist in the DB")
    return record


def touch(version_tag: str, document: Optional[VersionedDocument] = None,
          **details) -> None:
    """ Mark that data in `version_tag` changed: set its modification time
    and replace its cache token, and let `hooks.version_changed` receivers
    know. `document` is the one that was saved or deleted, if there was one;
    `details` are passed on to the receivers
    """
    found = VersionRecord._get_collection().find_one_and_update(
        {'version_tag': version_tag},
        {'$set': {'modified': datetime.datetime.now(),
                  'cache_token': ObjectId()}},
        projection={'_id': 1})
    hooks.version_changed.send(
        version_tag, record_id=None if found is None else found['_id'],
        document=document, **details)


def get_cache_token(version_tag: str) -> Optional[ObjectId]:
    """ Return a token that changes whenever data in `version_tag` changes,
    or None if the version doesn't exist
    """
    doc = VersionRecord.objects(version_tag=version_tag)\
                       .only('cache_token').as_pymongo().first()
    return None if doc is None else doc.get('cache_token')


def record_id(version_tag: str) -> Optional[ObjectId]:
    """ The id of the record of `version_tag`, or None """
    return VersionRecord.objects(version_tag=version_tag).scalar('id').first()


def _document_changed(sender, document, **kwargs):
    """ Saving or deleting any versioned document touches its version """
    if isinstance(document, VersionedDocument):
        version_tag = document.active_version
        if version_tag is not None:
            touch(version_tag, document)


def _record_saved(sender, document, **kwargs):
    if isinstance(document, VersionRecord):
        touch(document.version_tag)


# Not filtered by `sender`, which wouldn't match subclasses
signals.post_save.connect(_document_changed)
signals.post_delete.connect(_document_changed)
signals.post_save.connect(_record_saved)
