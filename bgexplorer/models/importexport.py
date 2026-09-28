""" Import documents from JSON files into a version as new items """
import io
import tarfile
import zipfile
from dataclasses import dataclass, field
from typing import IO, Iterator, List, Type

import mongoengine as me
from bson import DBRef, ObjectId, json_util
from mongoengine.base import get_document
from mongoengine.fields import (EmbeddedDocumentField, ListField, MapField,
                                DictField)

from .verdoc import (VersionedDocument, VersionedReferenceField,
                     strip_identity)

# derived fields that `clean` rebuilds
DERIVED_FIELDS = ('scalars_keys', 'spectra_keys', 'rois', 'cache_token')


def iter_json_documents(stream: IO[bytes],
                        filename: str = '') -> Iterator[dict]:
    """ Yield raw documents from a JSON file holding one document or a list
    of them, or from a tar or zip archive of such files
    """
    data = stream.read()
    if isinstance(data, str):
        data = data.encode()
    if data[:2] == b'\x1f\x8b' or data[257:262] == b'ustar':
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar:
                if member.isfile() and _is_json_name(member.name):
                    yield from _loads(tar.extractfile(member).read(),
                                      member.name)
    elif data[:4] == b'PK\x03\x04':
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                if not name.endswith('/') and _is_json_name(name):
                    yield from _loads(archive.read(name), name)
    else:
        yield from _loads(data, filename)


def _is_json_name(name: str) -> bool:
    """ Skip metadata files that archivers add """
    basename = name.rsplit('/', 1)[-1]
    return not basename.startswith('.') and '__MACOSX' not in name


def _loads(data: bytes, filename: str) -> Iterator[dict]:
    try:
        loaded = json_util.loads(data)
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"{filename or 'file'}: not valid JSON ({e})") from e
    for doc in loaded if isinstance(loaded, list) else [loaded]:
        if not isinstance(doc, dict):
            raise ValueError(f"{filename or 'file'}: expected JSON objects")
        yield doc


@dataclass
class ImportReport:
    """ The result of `import_documents` """
    created: List[VersionedDocument] = field(default_factory=list)
    # (document label, description of the dropped reference)
    dropped_refs: List[tuple] = field(default_factory=list)
    # (document label, error message)
    errors: List[tuple] = field(default_factory=list)


# marks a reference, or embedded document holding one, to be removed
_DROP = object()


def import_documents(doc_cls: Type[VersionedDocument], docs,
                     version: str) -> ImportReport:
    """ Save the raw documents `docs` in `version` as new items of `doc_cls`.

    References to documents imported together are relinked to the new
    copies. Other references must exist in `version`, or they are dropped
    and reported. Documents that fail to validate are reported in `errors`
    and don't stop the others.
    """
    report = ImportReport()
    pending = []  # (label, old id, son)
    idmap = {}  # old original_id: new original_id
    for index, son in enumerate(docs):
        son = dict(son)
        label = son.get('name') or son.get('_id') or f"#{index + 1}"
        label = str(label)
        cls_name = son.get('_cls', doc_cls._class_name)
        if cls_name not in doc_cls._subclasses:
            report.errors.append((label, f"is a {cls_name}, not a "
                                         f"{doc_cls._class_name}"))
            continue
        old_id = son.get('original_id') or son.get('_id')
        strip_identity(son)
        for key in DERIVED_FIELDS:
            son.pop(key, None)
        son['_id'] = son['original_id'] = ObjectId()
        if old_id is not None:
            idmap[old_id] = son['_id']
        pending.append((label, old_id, son))

    exists = {}  # cache of references found in the version

    def fix_ref(ref_field, value, label, path):
        ref_id = value.id if isinstance(value, DBRef) else value
        if ref_id in idmap:
            return DBRef(ref_field.document_type._get_collection_name(),
                         idmap[ref_id])
        target = ref_field.document_type
        key = (target._get_collection_name(), ref_id)
        if key not in exists:
            exists[key] = bool(target.objects(original_id=ref_id,
                                              version_tags=version).count())
        if exists[key]:
            return value
        report.dropped_refs.append(
            (label, f"{path}: no {target._class_name} {ref_id} "
                    f"in '{version}'"))
        return _DROP

    deps = {}
    for label, old_id, son in pending:
        refs = set()

        def fix(ref_field, value, path, label=label, refs=refs):
            ref_id = value.id if isinstance(value, DBRef) else value
            if ref_id in idmap:
                refs.add(idmap[ref_id])
            return fix_ref(ref_field, value, label, path)
        _walk_document(get_document(son.get('_cls', doc_cls._class_name)),
                       son, fix, '')
        deps[son['_id']] = refs

    # save documents after everything they refer to
    by_id = {son['_id']: (label, son) for label, _, son in pending}
    saved, failed = set(), set()
    remaining = list(by_id)
    while remaining:
        for i in remaining:
            if deps[i] & failed:
                failed.add(i)
                report.errors.append((by_id[i][0], "refers to a document "
                                                   "that failed to import"))
        remaining = [i for i in remaining if i not in failed]
        ready = [i for i in remaining if deps[i] <= saved]
        if not ready:
            for i in remaining:
                report.errors.append((by_id[i][0], "circular reference"))
            break
        for i in ready:
            remaining.remove(i)
            label, son = by_id[i]
            try:
                doc = doc_cls._from_son(son, created=True)
                doc.version_tags = [version]
                doc.active_version = version
                doc.save()
            except (me.ValidationError, me.errors.InvalidDocumentError,
                    ValueError, TypeError, me.DoesNotExist) as e:
                report.errors.append((label, str(e)))
                failed.add(i)
            else:
                saved.add(i)
                report.created.append(doc)
    return report


def _walk_document(doc_type, son: dict, fix, path: str) -> None:
    """ Replace references in the raw (embedded) document `son` in place
    with `fix(field, value, path)`. A reference that is _DROP'ed is removed
    """
    for name, fld in doc_type._fields.items():
        key = fld.db_field
        if key not in son or son[key] is None:
            continue
        value = _walk_field(fld, son[key], fix, f"{path}{name}")
        if value is _DROP:
            if fld.required:
                return _DROP
            del son[key]
        else:
            son[key] = value


def _walk_field(fld, value, fix, path):
    if isinstance(fld, VersionedReferenceField):
        return fix(fld, value, path)
    if isinstance(fld, EmbeddedDocumentField):
        if not isinstance(value, dict):
            return value
        doc_type = fld.document_type
        if value.get('_cls') in doc_type._subclasses:
            doc_type = get_document(value['_cls'])
        return _walk_document(doc_type, value, fix, path + '.') or value
    if isinstance(fld, (MapField, DictField)) and fld.field is not None:
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            item = _walk_field(fld.field, item, fix, f"{path}.{key}")
            if item is not _DROP:
                result[key] = item
        return result
    if isinstance(fld, ListField) and fld.field is not None:
        if not isinstance(value, list):
            return value
        result = []
        for index, item in enumerate(value):
            item = _walk_field(fld.field, item, fix, f"{path}.{index}")
            if item is not _DROP:
                result.append(item)
        return result
    return value
