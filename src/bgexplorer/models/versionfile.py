""" Export a whole version to a file, and import it as a new version

The file is a gzipped tar archive of:
    manifest.json: what the file holds, see `MANIFEST_FORMAT`
    settings.json: the version's VersionSettings
    <collection>.jsonl: one MongoDB Extended JSON document per line, for each
        versioned class except the SourceTerms, which are rebuilt on import

Documents keep their `original_id`, so the imported version's items are the
same items as the exported ones, and references between them still work.
Each copy gets a new `_id`, and belongs only to the imported version.
"""
import datetime
import importlib.metadata
import io
import json
import logging
import tarfile
import tempfile
from typing import IO, Dict, List, Optional

from bson import ObjectId, json_util
from mongoengine.base import get_document
from mongoengine.errors import ValidationError, InvalidDocumentError

from . import versioncontrol as vc
from .history import EventAction, log_event
from .settings import VersionSettings, VersionLock, holding
from .verdoc import current_user_name
from .versiondiff import find_refs, SETTINGS_IDENTITY_FIELDS
log = logging.getLogger(__name__)

MANIFEST_FORMAT = 'bgexplorer-version'
FORMAT_VERSION = 1
MANIFEST_NAME = 'manifest.json'
SETTINGS_NAME = 'settings.json'
# fields that identify a copy in its database
COPY_FIELDS = ('_id', 'version_tags')
JSON_OPTIONS = json_util.RELAXED_JSON_OPTIONS
INSERT_BATCH = 500
# report at most this many problems with a file
MAX_PROBLEMS = 20


class VersionFileError(ValueError):
    """ Raised when a version file can't be imported. `problems` lists the
    reasons
    """
    def __init__(self, message: str, problems: Optional[List[str]] = None):
        super().__init__(message)
        self.problems = problems or []


def _classes() -> Dict[str, type]:
    """ {tar member name: class} for the exported classes """
    return {f"{cls._get_collection_name()}.jsonl": cls
            for cls in vc._referenced_classes}


def _add_member(tar: tarfile.TarFile, name: str, fileobj: IO[bytes],
                size: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = int(datetime.datetime.now().timestamp())
    info.mode = 0o644
    fileobj.seek(0)
    tar.addfile(info, fileobj)


def export_version(version_tag: str, fileobj: IO[bytes]) -> dict:
    """ Write `version_tag` to `fileobj` as a gzipped tar archive. Returns
    the manifest. Raises KeyError if the version doesn't exist
    """
    vc.verify_version(version_tag)
    settings = VersionSettings._get_collection().find_one(
        {'version_tag': version_tag})
    description = settings.get('description')
    counts = {}
    with tarfile.open(fileobj=fileobj, mode='w|gz') as tar:
        for name, cls in _classes().items():
            count = 0
            with tempfile.SpooledTemporaryFile(max_size=2**24) as tmp:
                for son in cls._get_collection().find(
                        {'version_tags': version_tag}).sort('original_id'):
                    for key in COPY_FIELDS:
                        son.pop(key, None)
                    tmp.write(json_util.dumps(son, json_options=JSON_OPTIONS)
                              .encode() + b'\n')
                    count += 1
                _add_member(tar, name, tmp, tmp.tell())
            counts[cls.__name__] = count
        for key in SETTINGS_IDENTITY_FIELDS:
            settings.pop(key, None)
        data = json_util.dumps(settings, json_options=JSON_OPTIONS,
                               indent=1).encode()
        _add_member(tar, SETTINGS_NAME, io.BytesIO(data), len(data))
        manifest = dict(format=MANIFEST_FORMAT, format_version=FORMAT_VERSION,
                        bgexplorer_version=importlib.metadata.version(
                            'bgexplorer'),
                        version_tag=version_tag, description=description,
                        exported=datetime.datetime.now().isoformat(
                            timespec='seconds'),
                        counts=counts)
        data = json.dumps(manifest, indent=1).encode()
        _add_member(tar, MANIFEST_NAME, io.BytesIO(data), len(data))
    log.info(f"Exported version '{version_tag}': {counts}")
    return manifest


def read_version_file(fileobj: IO[bytes]) -> tuple:
    """ Read and check a version file without changing the database.
    Returns (manifest, settings, {class: [raw documents]}). Raises
    VersionFileError listing everything wrong with it
    """
    classes = _classes()
    members = {}
    try:
        with tarfile.open(fileobj=fileobj, mode='r|*') as tar:
            for member in tar:
                if member.name in members:
                    raise VersionFileError(f"'{member.name}' is in the file "
                                           "twice")
                if not member.isfile() or member.name not in (
                        set(classes) | {MANIFEST_NAME, SETTINGS_NAME}):
                    raise VersionFileError(f"Unexpected '{member.name}' in "
                                           "the file")
                members[member.name] = tar.extractfile(member).read()
    except (tarfile.TarError, EOFError, OSError) as e:
        raise VersionFileError(f"Not a version file: {e}") from e
    for name in (MANIFEST_NAME, SETTINGS_NAME):
        if name not in members:
            raise VersionFileError(f"Not a version file: no {name}")
    try:
        manifest = json.loads(members.pop(MANIFEST_NAME))
        settings = json_util.loads(members.pop(SETTINGS_NAME))
    except (ValueError, UnicodeDecodeError) as e:
        raise VersionFileError(f"Not a version file: {e}") from e
    if not isinstance(manifest, dict) \
            or manifest.get('format') != MANIFEST_FORMAT:
        raise VersionFileError("Not a version file: unknown format")
    fileversion = manifest.get('format_version')
    if not isinstance(fileversion, int) or fileversion > FORMAT_VERSION:
        raise VersionFileError(f"The file has format version {fileversion}; "
                               f"this server reads up to {FORMAT_VERSION}")
    if not isinstance(settings, dict):
        raise VersionFileError("settings.json must hold an object")

    problems = []
    docs = {}
    for name, cls in classes.items():
        docs[cls] = []
        seen = set()
        for lineno, line in enumerate(members.get(name, b'').splitlines(), 1):
            where = f"{name} line {lineno}"
            if not line.strip():
                continue
            try:
                son = json_util.loads(line)
            except (ValueError, UnicodeDecodeError) as e:
                problems.append(f"{where}: not valid JSON ({e})")
                continue
            if not isinstance(son, dict):
                problems.append(f"{where}: not a JSON object")
                continue
            clsname = son.get('_cls', cls._class_name)
            if clsname not in cls._subclasses:
                problems.append(f"{where}: a {clsname} isn't a "
                                f"{cls.__name__}")
                continue
            original_id = son.get('original_id')
            if not isinstance(original_id, ObjectId):
                problems.append(f"{where}: no original_id")
                continue
            if original_id in seen:
                problems.append(f"{where}: {original_id} is in the file "
                                "twice")
                continue
            seen.add(original_id)
            for key in COPY_FIELDS:
                son.pop(key, None)
            try:
                doc = get_document(clsname)._from_son(dict(son))
                doc.validate(clean=False)
            except (ValidationError, InvalidDocumentError, ValueError,
                    TypeError, KeyError) as e:
                problems.append(f"{where} ({son.get('name') or original_id})"
                                f": {e}")
                continue
            docs[cls].append(son)
        expected = manifest.get('counts', {}).get(cls.__name__, 0)
        if len(seen) != expected:
            problems.append(f"{name}: the manifest lists {expected} "
                            f"documents, but there are {len(seen)}")
    # everything referred to must be in the file
    ids = {son['original_id'] for sons in docs.values() for son in sons}
    for cls, sons in docs.items():
        for son in sons:
            if missing := set(find_refs(son)) - ids:
                problems.append(
                    f"{cls.__name__} {son.get('name') or son['original_id']}"
                    f" refers to documents that aren't in the file: "
                    + ', '.join(sorted(map(str, missing))))
    if problems:
        more = len(problems) - MAX_PROBLEMS
        problems = problems[:MAX_PROBLEMS] + (
            [f"and {more} more"] if more > 0 else [])
        raise VersionFileError("The file can't be imported", problems)
    return manifest, settings, docs


def import_version(fileobj: IO[bytes], version_tag: str,
                   editable: bool = True,
                   description: Optional[str] = None) -> VersionSettings:
    """ Create the new version `version_tag` from a file written by
    `export_version`, and rebuild its SourceTerms. Raises ValueError for a
    bad name, KeyError if the version exists and VersionFileError if the
    file can't be imported; the database is unchanged in all cases
    """
    from .maintenance import rebuild_sourceterms
    vc.validate_version_name(version_tag)
    vc.verify_version(version_tag, want_exists=False)
    manifest, settings, docs = read_version_file(fileobj)
    source = manifest.get('version_tag')
    if description is None:
        description = f"Imported from '{source}' ({manifest.get('exported')})"
    for key in SETTINGS_IDENTITY_FIELDS:
        settings.pop(key, None)
    settings.update(version_tag=version_tag, editable=True,
                    description=description,
                    modified=datetime.datetime.now(),
                    cache_token=ObjectId())
    try:
        VersionSettings._from_son(dict(settings)).validate(clean=False)
    except (ValidationError, InvalidDocumentError, ValueError, TypeError,
            KeyError) as e:
        raise VersionFileError("The file can't be imported",
                               [f"settings.json: {e}"]) from e
    # the new version is locked from the start, until it is complete
    lock = VersionLock(token=ObjectId(), reason="an import",
                       user=current_user_name())
    settings['lock'] = lock.to_mongo().to_dict()
    VersionSettings._get_collection().insert_one(settings)
    try:
        with holding({version_tag: lock.token}):
            for cls, sons in docs.items():
                coll = cls._get_collection()
                for start in range(0, len(sons), INSERT_BATCH):
                    batch = sons[start:start + INSERT_BATCH]
                    for son in batch:
                        son['_id'] = ObjectId()
                        son['version_tags'] = [version_tag]
                    coll.insert_many(batch)
            rebuild_sourceterms(version_tag)
            if not editable:
                VersionSettings._get_collection().update_one(
                    {'version_tag': version_tag},
                    {'$set': {'editable': False}})
    except Exception as e:
        log.exception(f"Importing '{version_tag}' failed, removing it")
        vc._delete_version(version_tag)
        raise VersionFileError(f"Importing failed: {e}", [str(e)]) from e
    counts = {cls.__name__: len(sons) for cls, sons in docs.items()}
    log_event(EventAction.import_version, version_tag, source,
              message=', '.join(f"{n} {name}" for name, n in counts.items()
                                if n),
              counts=counts, exported=manifest.get('exported'),
              editable=editable)
    return VersionSettings.objects.get(version_tag=version_tag)
