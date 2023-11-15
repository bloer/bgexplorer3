""" Define VersionedDocument class """
from mongoengine import (Document, ReferenceField, ObjectIdField,
                         StringField, QuerySet, IntField, DateTimeField,
                         signals, ValidationError, ListField)
from bson import ObjectId, DBRef
import datetime
from typing import Optional, List


class VersionedQuerySet(QuerySet):
    """ Override queryset to keep track of the currently active tag """
    def __init__(self, *args, active_version: Optional[str] = None, **kwargs):
        """ initialize and set active_version """
        self.active_version = active_version
        super().__init__(*args, **kwargs)

    def __next__(self):
        result = super().__next__()
        if not self._scalar:
            result.active_version = self.active_version
        return result

    def _clone_into(self, *args, **kwargs):
        result = super()._clone_into(*args, **kwargs)
        result.active_version = self.active_version
        return result

    def __call__(self, q_obj=None, **query):
        """ check for any call to version_tags and set active_version """
        tag = query.get('version_tags', None)
        if isinstance(tag, str):
            self.active_version = tag
        return super().__call__(q_obj, **query)

    def select_version(self, tag: str) -> 'VersionedQuerySet':
        """ Filter the collection for the given tag and set active_version """
        # Todo: do we need to clear any existing calls here?
        return self(version_tags=tag)

    def select_tag(self, tag: str) -> 'VersionedQuerySet':
        """ alias for select_version """
        return self.select_version(tag)


class VersionedDocument(Document):
    """ Custom Document class that keeps track of prior versions tagged in
    the database. A verison tag is like a git tag; all collections should be
    tagged simultaneously to take a snapshot of the entire database.

    If the same document is used in more than one version,
    the currently active version is kept in `active_version`. This is then
    used when dereferencing VersionedReferenceFields to select the same
    version from that collection.
    """

    __slots__ = ['_active_version']
    _DEFAULT_TAG = ''

    id = ObjectIdField(primary_key=True, default=ObjectId)
    original_id = ObjectIdField()
    version_tags = ListField(StringField(),
                             default=lambda: [VersionedDocument._DEFAULT_TAG],
                             unique_with='original_id')
    revision = IntField(default=0)
    modified = DateTimeField(default=datetime.datetime.now)

    meta = {'abstract': True, 'queryset_class': VersionedQuerySet}

    def __init__(self, *args, version_tag: Optional[str] = None,
                 active_version: Optional[str] = None, **kwargs):
        # TODO: do we want to allow setting version tag on create?
        if version_tag is not None:
            kwargs['version_tags'] = [version_tag]
            if active_version is None:
                active_version = version_tag
        self._active_version = active_version
        super().__init__(*args, **kwargs)

    @property
    def created(self) -> datetime.datetime:
        """ Get when this version of the document was created """
        return self.id.generation_time

    @property
    def original_created(self) -> datetime.datetime:
        """ Get when the original version of this document was created """
        try:
            return self.original_id.generation_time
        except AttributeError:
            # original id is None, document not saved yet
            pass
        return self.created

    @property
    def active_version(self) -> Optional[str]:
        """ Get the currently active version. If there is only one tag
        associated with this object, just return that. Otherwise will return
        None unless active_version has been set explicitly
        """
        if len(self.version_tags) == 1:
            return self.version_tags[0]
        return self._active_version

    @active_version.setter
    def active_version(self, tag: Optional[str]):
        """ Set the active version. Raises KeyError if `tag` is not in
        `self.version_tags`
        """
        if tag is not None and tag not in self.version_tags:
            raise KeyError(f"'{tag}' is not in this object's version_tags")
        self._active_version = tag

    @classmethod
    def create_tag(cls, newtag: str, fromtag: str = _DEFAULT_TAG) -> None:
        """ Create a new collection-wide version tag. For every document
        in the collection with `fromtag`, add `newtag` to its tags list.
        """
        cls.objects(version_tags=fromtag).update(push__version_tags=newtag)

    @classmethod
    def delete_tag(cls, tag: str) -> None:
        """ Remove the version tag `tag` from all documents in the collection
        If any documents contain *only* this tag, they are deleted.
        """
        cls.objects(version_tags=tag, version_tags__size=1).delete()
        cls.objects(version_tags=tag).update(pull__version_tags=tag)

    @classmethod
    def list_tags(cls) -> List[str]:
        """ Get a list of all version tags in the collection """
        return cls.objects.distinct('version_tags')

    @classmethod
    def select_version(cls, tag: str) -> VersionedQuerySet:
        """ See `VersionedQuerySet.select_version` """
        return cls.objects.select_version(tag)

    @classmethod
    def select_tag(cls, tag: str) -> VersionedQuerySet:
        """ See `VersionedQuerySet.select_tag` """
        return cls.objects.select_tag(tag)

    def clean(self) -> None:
        """ called during validation.
        Make sure original_id and active_version are both set
        """
        if not self.original_id:
            self.original_id = self.id
        if self.active_version is None:
            raise ValidationError("active_version must be set before saving")


class VersionedDynamicDocument(VersionedDocument):
    _dynamic = True

    def __delattr__(self, *args, **kwargs):
        """Delete the attribute by setting to None and allowing _delta
        to unset it.
        """
        field_name = args[0]
        if field_name in self._dynamic_fields:
            setattr(self, field_name, None)
            self._dynamic_fields[field_name].null = False
        else:
            super().__delattr__(*args, **kwargs)


def pre_save_post_validation(sender, document=None, created=False, **kwargs):
    """ Called by signals immediately prior to saving this object in the
    database. If multiple versions refer to this document, we split
    the active_version from the document in the database, then give this
    document a new id.
    """
    if document is None or not isinstance(document, VersionedDocument):
        return
    # if this version already exists in the db with multiple tags,
    # we need to remove this tag from the previous
    if len(document.version_tags) > 1:
        newtags = [document.active_version]
        dbversion = sender.objects(id=document.id)\
                          .modify(pull__version_tags=document.active_version)
        if dbversion and not created:
            # expect to update, so need a new entry
            dbversion.version_tags = newtags
            dbversion.id = None
            dbversion._created = True
            # TODO: need some error handling
            sender.objects.insert(dbversion)
            document.id = dbversion.id
        document.version_tags = newtags
    document.revision += 1
    document.modified = datetime.datetime.now()


signals.pre_save_post_validation.connect(pre_save_post_validation)


class VersionedReferenceField(ReferenceField):
    """ A RefernceField that looks up the referred document by
    (original_id, version_tag) rather then by _id.
    Active_version is explicitly never saved to the db, because we want to
    reference the owner's currently selected active version

    Reverse delete rules have not been tested and will probably not work!
    """
    def __init__(self, document_type, *args, **kwargs):
        if not isinstance(document_type, str) and not issubclass(
            document_type, VersionedDocument
        ):
            self.error(
                "Argument to VersionedReferenceField constructor must be a "
                "versioned document class or a string"
            )
        super().__init__(document_type, *args, **kwargs)

    def __get__(self, instance, owner):
        if instance is None:
            return self
        ref_value = instance._data.get(self.name)
        if isinstance(ref_value, DBRef):
            instance._data[self.name] = DBRef(
                ref_value.collection,
                ref_value.id,
                ref_value.database,
                active_version=instance.active_version
            )
        return super().__get__(instance, owner)

    @staticmethod
    def _lazy_load_ref(ref_cls, dbref):
        active_version = getattr(dbref, 'active_version', None)
        if active_version is not None:
            return ref_cls.objects.get(original_id=dbref.id,
                                       version_tags=active_version)

        return ReferenceField._lazy_load_ref(ref_cls, dbref)

    def validate(self, value):
        if isinstance(value, VersionedDocument) and value.original_id is None:
            self.error(
                "You can only reference documents once they have been "
                "saved to the database"
            )
        return super().validate(value)

    def to_mongo(self, document):
        if isinstance(document, VersionedDocument):
            document = DBRef(document._get_collection_name(),
                             document.original_id)
        return super().to_mongo(document)
