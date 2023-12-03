""" Define VersionedDocument class """
from mongoengine import (Document, ReferenceField, ObjectIdField,
                         StringField, QuerySet, IntField, DateTimeField,
                         signals, ValidationError, ListField)

from mongoengine import CASCADE, NULLIFY, PULL
from bson import ObjectId, DBRef
import datetime
from typing import Optional, List


# FIXME: need to override update, modify, etc
class VersionedQuerySet(QuerySet):
    """ Override queryset to keep track of the currently active tag """
    def __init__(self, *args, active_version: Optional[str] = None, **kwargs):
        """ initialize and set active_version """
        # TODO: should we always set a default active version?
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

    def clear_query(self):
        return self._document.objects()

    def select_version(self, tag: str) -> 'VersionedQuerySet':
        """ Filter the collection for the given tag and set active_version """
        # Todo: do we need to clear any existing calls here?
        return self(version_tags=tag)

    def select_tag(self, tag: str) -> 'VersionedQuerySet':
        """ alias for select_version """
        return self.select_version(tag)

    def delete(self, *args, bypass_version_control: bool = False, **kwargs):
        """ Override base delete if active_version is set """
        if self.active_version is not None and not bypass_version_control:
            # instead of directly deleting, pull the currently active tag from
            # all documents matching the filter. Then delete all documents
            # with empty version_tags
            qs = self.clone()
            count = qs.update(pull__version_tags=self.active_version,
                              bypass_version_control=True)
            # empty version tags objects are deleted by the update call
            return count
        # TODO: should this cause an error? how to prevent accidental
        # TODO: where to check if version is protected?
        return super().delete(*args, **kwargs)

    def prepare_edit(self) -> 'VersionedQuerySet':
        """ Find all documents matching the current query with more than
        one version_tag. Pop the current active_version from that document,
        then clone a new document with the same original_id and only the
        active version.

        Returns a queryset pointing to the same objects refernced by
        (original_id, version_tags=active_version)
        """
        if self.active_version is None:
            raise ValueError("Must set active_version before prepare_edit")

        # TODO: possible race condition, this should be a transaction

        # first, get the list of matching objects before we mess with things
        matchids = list(self.scalar('original_id'))
        # if not matchids:
        #    return self.clear_query().none()

        response = self.clear_query().filter(original_id__in=matchids,
                                             version_tags=self.active_version)

        # now find all matches with more than 1 version tag. Update the
        # existing one to remove the active version, then create a clone
        # with a new ID and active_version
        tobefixed = list(response(version_tags__1__exists=True).scalar('id'))
        if tobefixed:
            qs = self.clear_query()(id__in=tobefixed)
            qs.update(pull__version_tags=self.active_version,
                      bypass_version_control=True,
                      bypass_reverse_delete=True)
            qs.aggregate([{'$unset': '_id'},
                          {'$set': {'version_tags': [self.active_version]}},
                          {'$merge': qs._collection.name},
                          ])
        return response

    def handle_reverse_delete(self, write_concern=None):
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
                                cascade_refs=cascade_refs)
            elif rule == NULLIFY:
                document_cls.objects(**{field_name + "__in": self},
                                     **tag_query).update(
                    write_concern=write_concern,
                    **{"unset__%s" % field_name: 1}
                )
            elif rule == PULL:
                document_cls.objects(**{field_name + "__in": self},
                                     **tag_query).update(
                    write_concern=write_concern,
                    **{"pull_all__%s" % field_name: self}
                )

    def update(self, *args, bypass_version_control: bool = False,
               bypass_reverse_delete: bool = False, **kwargs):
        """ If trying to update a document with multiple version tags,
        we need to remove the active_version and switch to upsert.
        This will almost certainly cause problems if the update arguments
        don't fully-specify the document or rely on ID
        """
        qs = self.clone()
        if self.active_version is not None and not bypass_version_control:
            qs = self.prepare_edit()
            kwargs['inc__revision'] = 1
            kwargs['set__modified'] = datetime.datetime.now()

        delete_after = False
        if 'pull__version_tags' in kwargs:
            delete_after = True
            if not bypass_reverse_delete:
                # removing a version tag is equivalent to deleting
                write_concern = kwargs.get('write_concern')
                qs.handle_reverse_delete(write_concern=write_concern)

        count = QuerySet.update(qs, *args, **kwargs)
        if count and delete_after:
            # remove any object with empty version_tags
            self._collection.delete_many({'version_tags': {'$size': 0}})
        return count

    def modify(self, *args, bypass_version_control: bool = False, **kwargs):
        qs = self.clone()
        if self.active_version is not None and not bypass_version_control:
            qs = self.prepare_edit()
            kwargs['inc__revision'] = 1
            kwargs['set__modified'] = datetime.datetime.now()
        # TODO: this doesn't handle most of modify's args
        return QuerySet.modify(qs, *args, **kwargs)


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

    enteredby = StringField(verbose_name="Data entered by")

    meta = {'abstract': True, 'queryset_class': VersionedQuerySet,
            'indexes': ['version_tags'],
            }

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
        cls.objects(version_tags=fromtag).update(bypass_version_control=True,
                                                 push__version_tags=newtag)

    @classmethod
    def delete_tag(cls, tag: str) -> None:
        """ Remove the version tag `tag` from all documents in the collection
        If any documents contain *only* this tag, they are deleted.
        """
        cls.objects.select_tag(tag).delete()

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

    def clean(self) -> None:
        """ called during validation.
        Make sure original_id and active_version are both set
        """
        if not self.original_id:
            self.original_id = self.id
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


class DynamicVersionedDocument(VersionedDocument):
    _dynamic = True
    meta = {'abstract': True, 'queryset_class': VersionedQuerySet}

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


signals.pre_save_post_validation.connect(pre_save_post_validation)


class VersionedReferenceField(ReferenceField):
    """ A RefernceField that looks up the referred document by
    (original_id, version_tag) rather then by _id.
    Active_version is explicitly never saved to the db, because we want to
    reference the owner's currently selected active version

    Reverse delete rules have not been tested and will probably not work!
    """
    # TODO: handle reverse_delete_rules
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


"""
        I thought this would be a good way to do it atomically, but now I'm
        not sure if it would work at all

        qs = self.clone()
        filt = {'input': '$version_tags',
                'as': 'this',
                'cond': {'$ne': ['$$this', self.active_version]},
                }
        qs(version_tags__size__gt=1).aggregate([
            # split version_tags into nested arrays with active and others
            {'$set': {'version_tags': [[self.active_version],
                                       {'$filter': filt}]},
            # make a new document for active
            {'$unwind': '$version_tags'},
            # delete the _id for the active tag
            {'$set': {'_id': {'$cond': {
                'if': {'$eq': ['$version_tags', [self.active_version]]},
                'then': '$$REMOVE',
                'else': '$_id',
                },
             },
             # write back to the collection, copying the  new document
             # and upating tags on old
             # this will attempt to merge the whole thing, is that
             # inefficient, compared to update?
             # plus if the new insert happens before the merge, it will
             # violate unique constraints...
             {'$merge': self._collection.name}
            ])
        """
