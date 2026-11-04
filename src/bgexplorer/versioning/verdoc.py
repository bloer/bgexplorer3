""" Define VersionedDocument class """
from mongoengine import (Document, ReferenceField, ObjectIdField,
                         StringField, QuerySet, IntField, DateTimeField,
                         signals, ValidationError, ListField,
                         EmbeddedDocumentListField)
from mongoengine.dereference import DeReference
from mongoengine.queryset.field_list import QueryFieldList

from mongoengine import CASCADE, NULLIFY, PULL
from bson import ObjectId, DBRef
import datetime
from pymongo.errors import DuplicateKeyError
from typing import Optional, List, Callable


class ReadOnlyVersionError(PermissionError):
    """ Raised when trying to modify documents in a read-only version (tag) """


class VersionLockedError(ReadOnlyVersionError):
    """ Raised when trying to modify a version locked by another operation,
    such as a merge """


class LockLostError(VersionLockedError):
    """ Raised when the operation that locked a version tries to write to it
    after its lock was cleared or taken over, e.g. because it expired """


# Checks that a version may be written. Installed by `locks`, which owns
# the data that decides it, so this module doesn't depend on it
_write_guard: Callable[[str], None] = lambda version_tag: None


def set_write_guard(guard: Callable[[str], None]) -> None:
    """ Install the function that raises ReadOnlyVersionError or
    VersionLockedError if the given version can't be written """
    global _write_guard
    _write_guard = guard


def check_writable(version_tag: Optional[str]) -> None:
    """ Raise ReadOnlyVersionError if `version_tag` is a read-only version,
    or VersionLockedError if it is locked by another operation.
    Versions without a record are writable
    """
    if version_tag is not None:
        _write_guard(version_tag)


def _readonly_kwargs(document_cls, bypass_readonly: bool) -> dict:
    """ Pass bypass_readonly on only to versioned querysets """
    if bypass_readonly and issubclass(document_cls, VersionedDocument):
        return {'bypass_readonly': True}
    return {}


class VersionedQuerySet(QuerySet):
    """ Override queryset to keep track of the currently active tag """
    def __init__(self, *args, active_version: Optional[str] = None, **kwargs):
        """ initialize and set active_version """
        self.active_version = active_version
        super().__init__(*args, **kwargs)

    def _loaded(self, doc):
        """ Set active_version on `doc` and remember which fields it was
        loaded with, see VersionedDocument.is_loaded
        """
        if isinstance(doc, VersionedDocument):
            doc.active_version = self.active_version
            if self._loaded_fields:
                doc._query_fields = self._loaded_fields
        return doc

    def __next__(self):
        result = super().__next__()
        if not self._scalar:
            self._loaded(result)
        return result

    def __getitem__(self, key):
        result = super().__getitem__(key)
        if isinstance(key, int) and not self._scalar:
            self._loaded(result)
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

    def clear_query(self):
        return self._document.objects()

    def select_version(self, tag: str) -> 'VersionedQuerySet':
        """ Filter the collection for the given tag and set active_version """
        # Todo: do we need to clear any existing calls here?
        return self(version_tags=tag)

    def select_tag(self, tag: str) -> 'VersionedQuerySet':
        """ alias for select_version """
        return self.select_version(tag)

    def delete(self, *args, bypass_version_control: bool = False,
               bypass_reverse_delete: bool = False,
               bypass_readonly: bool = False, **kwargs):
        """ Override base delete if active_version is set. Raises
        ReadOnlyVersionError if active_version is read-only, unless
        `bypass_readonly` is set
        """
        if not bypass_readonly:
            check_writable(self.active_version)
        if self.active_version is not None and not bypass_version_control:
            # instead of directly deleting, pull the currently active tag from
            # all documents matching the filter. Then delete all documents
            # with empty version_tags
            qs = self.clone()
            count = qs.update(pull__version_tags=self.active_version,
                              bypass_version_control=True,
                              bypass_reverse_delete=bypass_reverse_delete,
                              bypass_readonly=bypass_readonly)
            # empty version tags objects are deleted by the update call
            return count
        # without an active version, this is an administrative delete from
        # every version; protected versions are checked in `control`
        return super().delete(*args, **kwargs)

    def prepare_edit(self) -> 'VersionedQuerySet':
        """ Find all documents matching the current query with more than
        one version_tag. Pop the current active_version from that document,
        then clone a new document with the same original_id and only the
        active version.

        Returns a queryset pointing to the same objects refernced by
        (original_id, version_tags=active_version)

        Without transactions the split is done in an order that never loses
        a document: each copy is made under the staging tag of the version,
        the active version is removed from the originals, and only then do
        the copies take it. The unique index on (version_tags, original_id)
        lets only one writer stage a copy of a document, so a writer that
        finds one raises VersionLockedError, unless it is older than
        `EDIT_GRACE`, when it is left from a failed edit and replaced. A
        failure after the originals were changed leaves staged copies that
        `maintenance.recover_interrupted_edits` restores or discards.
        """
        active = self.active_version
        if active is None:
            raise ValueError("Must set active_version before prepare_edit")

        # first, get the list of matching objects before we mess with things
        matchids = list(self.scalar('original_id'))
        response = self.clear_query().filter(original_id__in=matchids,
                                             version_tags=active)

        # now find all matches with more than 1 version tag
        shared = list(response(version_tags__1__exists=True)
                      .scalar('id', 'original_id'))
        if shared:
            staging = staging_tag(active)
            staged = self._stage_copies(shared, staging)
            self.clear_query()(id__in=[i for i, _ in shared]).update(
                pull__version_tags=active, bypass_version_control=True,
                bypass_reverse_delete=True)
            self.clear_query()(__raw__={
                'original_id': {'$in': staged}, 'version_tags': [staging]}
            ).update(set__version_tags=[active], bypass_version_control=True)
        return response

    def _stage_copies(self, shared, staging: str) -> list:
        """ Copy the documents `shared`, a list of (id, original_id), with
        only the tag `staging`. Returns the original_ids copied. If any can't
        be copied the copies made are removed again
        """
        coll = self._collection
        staged = []
        try:
            for _id, original_id in shared:
                self._stage_copy(_id, original_id, staging)
                staged.append(original_id)
        except BaseException:
            coll.delete_many({'original_id': {'$in': staged},
                              'version_tags': [staging]})
            raise
        return staged

    def _stage_copy(self, _id, original_id, staging: str) -> None:
        source = self.clear_query()(id=_id)
        pipeline = [{'$unset': '_id'},
                    {'$set': {'version_tags': [staging]}},
                    {'$merge': self._collection.name}]
        for retry in (True, False):
            try:
                source.aggregate(pipeline)
                return
            except DuplicateKeyError:
                blocking = self._collection.find_one(
                    {'original_id': original_id, 'version_tags': [staging]},
                    {'_id': 1})
                if blocking is None:
                    raise
                if not retry or not staged_edit_is_stale(blocking['_id']):
                    raise VersionLockedError(
                        "The document is being edited by someone else, "
                        "try again later") from None
                self._collection.delete_one({'_id': blocking['_id'],
                                             'version_tags': [staging]})

    def handle_reverse_delete(self, write_concern=None,
                              bypass_readonly: bool = False):
        # this is copied straight from BaseQueryset
        queryset = self.clone()
        doc = queryset._document
        delete_rules = doc._meta.get("delete_rules") or {}
        delete_rules = list(delete_rules.items())
        for rule_entry, rule in delete_rules:
            document_cls, field_name = rule_entry
            if document_cls._meta.get("abstract"):
                continue
            tag_query = dict()
            if (self.active_version is not None and
                    issubclass(document_cls, VersionedDocument)):
                tag_query['version_tags'] = self.active_version

            if rule == CASCADE:
                cascade_refs = set()
                # Handle recursive reference
                if doc._collection == document_cls._collection:
                    for ref in queryset:
                        cascade_refs.add(ref.id)
                refs = document_cls.objects(
                    **tag_query,
                    **{field_name + "__in": self, "pk__nin": cascade_refs}
                )
                if refs.count() > 0:
                    refs.delete(write_concern=write_concern,
                                cascade_refs=cascade_refs,
                                **_readonly_kwargs(document_cls,
                                                   bypass_readonly))
            elif rule == NULLIFY:
                document_cls.objects(**{field_name + "__in": self},
                                     **tag_query).update(
                    write_concern=write_concern,
                    **_readonly_kwargs(document_cls, bypass_readonly),
                    **{"unset__%s" % field_name: 1}
                )
            elif rule == PULL:
                document_cls.objects(**{field_name + "__in": self},
                                     **tag_query).update(
                    write_concern=write_concern,
                    **_readonly_kwargs(document_cls, bypass_readonly),
                    **{"pull_all__%s" % field_name: self}
                )

    def update(self, *args, bypass_version_control: bool = False,
               bypass_reverse_delete: bool = False,
               bypass_readonly: bool = False, **kwargs):
        """ If trying to update a document with multiple version tags,
        we need to remove the active_version and switch to upsert.
        This will almost certainly cause problems if the update arguments
        don't fully-specify the document or rely on ID
        """
        qs = self.clone()
        if self.active_version is not None and not bypass_version_control:
            if not bypass_readonly:
                check_writable(self.active_version)
            qs = self.prepare_edit()
            kwargs['inc__revision'] = 1
            kwargs['set__modified'] = datetime.datetime.now()

        pulled = None
        if 'pull__version_tags' in kwargs:
            # what may end up with no tags, found before the pull hides it
            pulled = list(qs.scalar('id'))
            if not bypass_reverse_delete:
                # removing a version tag is equivalent to deleting
                write_concern = kwargs.get('write_concern')
                qs.handle_reverse_delete(write_concern=write_concern,
                                         bypass_readonly=bypass_readonly)

        count = QuerySet.update(qs, *args, **kwargs)
        if count and pulled:
            # remove what has no version tags left
            self._collection.delete_many({'_id': {'$in': pulled},
                                          'version_tags': {'$size': 0}})
        return count

    def modify(self, *args, bypass_version_control: bool = False, **kwargs):
        qs = self.clone()
        if self.active_version is not None and not bypass_version_control:
            check_writable(self.active_version)
            qs = self.prepare_edit()
            kwargs['inc__revision'] = 1
            kwargs['set__modified'] = datetime.datetime.now()
        # only the update is versioned, other arguments pass through
        return QuerySet.modify(qs, *args, **kwargs)


# Copies being split off a shared document are tagged `editing/<version>`
# until they take the version. Version names can't contain '/', so these
# can't be the tag of a version. The age of the copy is the time in its id.
STAGING_PREFIX = 'editing/'

# staged copies younger than this may still belong to a running edit
EDIT_GRACE = datetime.timedelta(minutes=1)


def staging_tag(version_tag: str) -> str:
    return f'{STAGING_PREFIX}{version_tag}'


def parse_staging_tag(tag: str) -> Optional[str]:
    """ The version of a tag made by `staging_tag`, or None for any other tag
    """
    if tag.startswith(STAGING_PREFIX) and len(tag) > len(STAGING_PREFIX):
        return tag[len(STAGING_PREFIX):]
    return None


def staged_edit_is_stale(doc_id: ObjectId,
                         grace: Optional[datetime.timedelta] = None) -> bool:
    """ Whether the staged copy `doc_id` was made more than `grace`
    (`EDIT_GRACE` by default) ago
    """
    grace = EDIT_GRACE if grace is None else grace
    made = doc_id.generation_time.replace(tzinfo=None)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return made <= now - grace


# every subclass of VersionedDocument, in definition order
_defined: list = []


class VersionedDocument(Document):
    """ Custom Document class that keeps track of prior versions tagged in
    the database. A verison tag is like a git tag; all collections should be
    tagged simultaneously to take a snapshot of the entire database.

    If the same document is used in more than one version,
    the currently active version is kept in `active_version`. This is then
    used when dereferencing VersionedReferenceFields to select the same
    version from that collection.
    """

    __slots__ = ['_active_version', '_query_fields']
    _DEFAULT_TAG = 'main'

    # Whether version control manages the collection: copies it for new
    # versions, deletes from it, checks and compares it, and so on. Subclasses
    # that share their parent's collection are covered by the parent.
    VERSION_REGISTER = True
    # Whether the documents are computed from those of other classes, so that
    # comparing, merging and exporting versions leave them out
    VERSION_DERIVED = False
    # Fields (dotted paths) that are too large to load when comparing or
    # merging versions
    VERSION_BULKY_FIELDS = ()
    # Fields that name a document when listing the differences between
    # versions, see `item_label`
    VERSION_LABEL_FIELDS = ('name',)

    id = ObjectIdField(primary_key=True, default=ObjectId)
    original_id = ObjectIdField()
    version_tags = ListField(StringField(),
                             default=lambda: [VersionedDocument._DEFAULT_TAG],
                             unique_with='original_id')
    revision = IntField(default=0)
    modified = DateTimeField(default=datetime.datetime.now)

    enteredby = StringField(verbose_name="Data entered by")

    meta = {'abstract': True, 'queryset_class': VersionedQuerySet,
            'indexes': ['version_tags', 'modified'],
            'ordering': ['-modified'],
            }

    def __init_subclass__(cls, **kwargs):
        # The metaclass hasn't set up cls._meta yet, so only note the class
        # here. versioned_classes() works out the collections when asked
        super().__init_subclass__(**kwargs)
        _defined.append(cls)

    def __init__(self, *args, version_tag: Optional[str] = None,
                 active_version: Optional[str] = None, **kwargs):
        if version_tag is not None:
            kwargs['version_tags'] = [version_tag]
            if active_version is None:
                active_version = version_tag
        self._active_version = active_version
        # set by VersionedQuerySet if only() or exclude() was used
        self._query_fields = None
        super().__init__(*args, **kwargs)

    def is_loaded(self, field: str) -> bool:
        """ False if `field` was left out, entirely or in part, by only()
        or exclude() in the query that loaded this document
        """
        fields = self._query_fields
        if not fields or not fields.fields:
            return True
        name = self._fields[field].db_field if field in self._fields else field
        if fields.value == QueryFieldList.ONLY:
            return name in fields.fields
        return not any(f == name or f.startswith(name + '.')
                       for f in fields.fields)

    def other_versions(self) -> List[dict]:
        """ One row per version containing this item (any copy with the same
        original_id), sorted by version name. Each row has `version`,
        `revision`, `modified`, `enteredby`, `current` (the active version)
        and `same_copy` (the version shares this physical copy)
        """
        if self.original_id is None:
            return []
        copies = (type(self).objects(original_id=self.original_id)
                  .only('version_tags', 'revision', 'modified', 'enteredby'))
        rows = [dict(version=tag, revision=copy.revision,
                     modified=copy.modified, enteredby=copy.enteredby,
                     current=tag == self.active_version,
                     same_copy=copy.id == self.id)
                for copy in copies for tag in copy.version_tags]
        return sorted(rows, key=lambda row: row['version'])

    def clone(self, **overrides) -> 'VersionedDocument':
        """ An unsaved copy of this document in its active version, as a new
        item with new ids for embedded documents. References are kept, so the
        copy of an Assembly shares its children. Attributes are set from
        `overrides`.
        """
        if self._query_fields:
            raise ValueError("Can't clone a partly loaded document")
        son = strip_identity(self.to_mongo().to_dict())
        renew_embedded_ids(son)
        copy = type(self)._from_son(son, created=True)
        copy.version_tags = [self.active_version]
        copy.active_version = self.active_version
        for key, value in overrides.items():
            setattr(copy, key, value)
        return copy

    def reload(self, *fields, **kwargs):
        result = super().reload(*fields, **kwargs)
        if not fields:
            self._query_fields = None
        return result

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
        cls.objects(version_tags=fromtag).update(bypass_version_control=True,
                                                 add_to_set__version_tags=newtag)

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

    @classmethod
    def get_default_tag(cls):
        return cls._DEFAULT_TAG

    @classmethod
    def item_label(cls, son: dict) -> str:
        """ A name for the item in the raw document `son` that has the
        VERSION_LABEL_FIELDS, for listing differences between versions
        """
        return ' @ '.join(str(son[k]) for k in cls.VERSION_LABEL_FIELDS
                          if son.get(k)) or str(son['original_id'])

    @classmethod
    def find_dangling(cls, version_tag: str) -> list:
        """ The `_id`s of copies in `version_tag` that refer to items that
        aren't in it. Subclasses with references that need checking override
        this, see `maintenance.find_orphans`
        """
        return []

    def delete(self, *args, **kwargs):
        # documents with a single tag are deleted by pk, so the queryset
        # doesn't know the version
        check_writable(self.active_version)
        return super().delete(*args, **kwargs)

    def update(self, **kwargs):
        check_writable(self.active_version)
        return super().update(**kwargs)

    def modify(self, *args, **kwargs):
        check_writable(self.active_version)
        return super().modify(*args, **kwargs)

    def clean(self) -> None:
        """ called during validation.
        Make sure original_id, version_tags and active_version are all set
        """
        if not self.original_id:
            self.original_id = self.id
        check_version_tags(self)
        if self.active_version is None:
            raise ValidationError("active_version must be set before saving")

    @property
    def _object_key(self):
        """ If active_version is set, replace 'pk' in object key with
        original_id and version_tags.  This is so that when we call
        `delete`, the internally-generated queryset will have an
        active_version set, so will respect version control properly.

        If this contains a shard key it will probably not work...
        """
        select_dict = super()._object_key
        # note we check the private _active_version so objects with only one
        # version tag still reference by pk
        if self._active_version is not None:
            select_dict.pop('pk', None)
            select_dict['original_id'] = self.original_id
            select_dict['version_tags'] = self._active_version
        return select_dict


# fields that identify a stored copy of a document, not its content
IDENTITY_FIELDS = ('_id', 'original_id', 'version_tags', 'revision',
                   'modified')


def strip_identity(son: dict) -> dict:
    """ Remove IDENTITY_FIELDS from the raw document `son` in place """
    for key in IDENTITY_FIELDS:
        son.pop(key, None)
    return son


def renew_embedded_ids(son) -> None:
    """ Give every embedded document in the raw document `son` a new 'id'
    in place
    """
    children = son.values() if isinstance(son, dict) else son
    for child in children:
        if isinstance(child, dict):
            if isinstance(child.get('id'), ObjectId):
                child['id'] = ObjectId()
            renew_embedded_ids(child)
        elif isinstance(child, list):
            renew_embedded_ids(child)


class DynamicVersionedDocument(VersionedDocument):
    _dynamic = True
    meta = {'abstract': True, 'queryset_class': VersionedQuerySet}

    def __setattr__(self, name, value):
        """ Properties such as active_version are not dynamic fields, which
        would otherwise be created (and saved) for any public attribute.
        While loading, stale values saved that way are ignored
        """
        if isinstance(getattr(type(self), name, None), property):
            if getattr(self, '_initialised', False):
                object.__setattr__(self, name, value)
        else:
            super().__setattr__(name, value)

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


# returns the name of the user making changes, or None if unknown
_user_provider: Optional[Callable[[], Optional[str]]] = None


def set_user_provider(provider: Optional[Callable[[], Optional[str]]]
                      ) -> None:
    """ Saved documents get `enteredby` from `provider()`, unless it returns
    None. This keeps the model layer free of the web app's logins
    """
    global _user_provider
    _user_provider = provider


def current_user_name() -> Optional[str]:
    """ The name of the user making changes, if known """
    return _user_provider() if _user_provider is not None else None


def versioned_classes() -> List[type]:
    """ The classes whose collections are versioned: one for each collection,
    the base class if several share one. Documents that others refer to come
    first, then by name
    """
    classes = {}
    for cls in _defined:
        if cls._meta.get('abstract') or not cls.VERSION_REGISTER:
            continue
        classes.setdefault(cls._get_collection_name(), cls)
    return sorted(classes.values(),
                  key=lambda cls: (cls.VERSION_DERIVED, cls.__name__))


def primary_classes() -> List[type]:
    """ The versioned classes whose documents aren't derived from others """
    return [cls for cls in versioned_classes() if not cls.VERSION_DERIVED]


def check_version_tags(document: VersionedDocument) -> None:
    """ Raise ValidationError if `document` belongs to no version, since no
    version could see it once saved
    """
    if not document.version_tags:
        raise ValidationError("version_tags can't be empty",
                              field_name='version_tags')


def pre_save_post_validation(sender, document=None, created=False, **kwargs):
    """ Called by signals immediately prior to saving this object in the
    database. If multiple versions refer to this document, we split
    the active_version from the document in the database, then give this
    document a new id.
    """
    if document is None or not isinstance(document, VersionedDocument):
        return
    # also checked by clean, but this runs for save(validate=False) too
    check_version_tags(document)
    check_writable(document.active_version)
    # if this version already exists in the db with multiple tags,
    # we need to remove this tag from the previous, create a new one,
    # and update our ID to match the new one
    if not created and len(document.version_tags) > 1:
        sender.select_tag(document.active_version)\
              .filter(id=document.id).prepare_edit()
        document.id = sender.select_tag(document.active_version)\
                            .filter(original_id=document.original_id)\
                            .scalar('id').get()
        document.version_tags = [document.active_version]
    document.revision += 1
    document.modified = datetime.datetime.now()
    if name := current_user_name():
        document.enteredby = name


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
                active_version=get_active_version(instance)
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


def ref_id(ref):
    """ The stored id of a reference, which may not be dereferenced yet """
    if isinstance(ref, VersionedDocument):
        return ref.original_id
    if isinstance(ref, Document):
        return ref.id
    # DBRef or plain id
    return getattr(ref, 'id', ref)


def get_active_version(instance) -> Optional[str]:
    """ Get the active_version of `instance`. For EmbeddedDocuments, walk up
    the chain of owners to the root VersionedDocument. Returns None if no
    versioned owner can be found
    """
    while instance is not None and not isinstance(instance, VersionedDocument):
        instance = getattr(instance, '_instance', None)
    return None if instance is None else instance.active_version


class VersionedDeReference(DeReference):
    """ Bulk dereference that looks up VersionedDocuments by
    (original_id, active_version) rather than by _id.

    mongoengine dereferences all references inside a list or embedded
    document list in one bulk query by _id, which bypasses
    `VersionedReferenceField._lazy_load_ref`
    """
    def __init__(self, active_version: Optional[str] = None):
        self.active_version = active_version

    def _fetch_objects(self, doc_type=None):
        if self.active_version is None:
            return super()._fetch_objects(doc_type=doc_type)
        object_map = {}
        remaining = {}
        for collection, refs in self.reference_map.items():
            # plain lists of references are keyed by collection name
            doc_cls = collection
            if isinstance(collection, str) and isinstance(doc_type, type):
                doc_cls = doc_type
            if (isinstance(doc_cls, type)
                    and issubclass(doc_cls, VersionedDocument)):
                col_name = doc_cls._get_collection_name()
                docs = doc_cls.objects(original_id__in=list(refs),
                                       version_tags=self.active_version)
                for doc in docs:
                    object_map[(col_name, doc.original_id)] = doc
            else:
                remaining[collection] = refs
        self.reference_map = remaining
        object_map.update(super()._fetch_objects(doc_type=doc_type))
        return object_map


class _VersionedDereferenceMixin:
    """ Dereference with VersionedDeReference using the owner's
    active_version
    """
    @staticmethod
    def _lazy_load_refs(instance, name, ref_values, *, max_depth):
        dereference = VersionedDeReference(get_active_version(instance))
        return dereference(ref_values, max_depth=max_depth,
                           instance=instance, name=name)


class VersionedListField(_VersionedDereferenceMixin, ListField):
    """ ListField that dereferences VersionedReferenceFields according to
    the owner's active_version
    """


class VersionedEmbeddedDocumentListField(_VersionedDereferenceMixin,
                                         EmbeddedDocumentListField):
    """ EmbeddedDocumentListField that dereferences VersionedReferenceFields
    inside the embedded documents according to the owner's active_version
    """
