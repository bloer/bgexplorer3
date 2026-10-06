""" Database maintenance: statistics, consistency checks and cache control """
from dataclasses import dataclass, field
from typing import Dict, List
import logging
from .component import Component, Assembly
from .emissionspec import EmissionSpec
from .hiteff import HitEfficiency
from .sourceterm import SourceTerm, CalculatedResults, clear_results_cache
from .settings import VersionSettings, touch
from .verdoc import check_writable
from . import versioncontrol as vc
from . import signals
log = logging.getLogger(__name__)

VERSIONED_CLASSES = tuple(vc._versioned_classes)


def collection_size(doc_cls) -> Dict[str, int]:
    """ Document count and data size in bytes of a collection """
    coll = doc_cls._get_collection()
    try:
        stats = coll.database.command('collStats', coll.name)
        size = stats.get('size', 0)
    except Exception:  # not supported by every server
        size = None
    return dict(count=coll.estimated_document_count(), size=size)


def database_stats() -> dict:
    """ Per-version document counts and totals per collection """
    versions = {v.version_tag: dict(editable=v.editable,
                                    counts=vc.version_summary(v.version_tag))
                for v in vc.list_versions()}
    collections = {cls.__name__: collection_size(cls)
                   for cls in VERSIONED_CLASSES + (CalculatedResults,)}
    return dict(versions=versions, collections=collections)


@dataclass
class Orphans:
    """ Inconsistent documents found by `find_orphans` """
    # class name: ids of documents with no version tags
    untagged: Dict[str, List] = field(default_factory=dict)
    # version: ids of SourceTerms whose component or spec is missing there
    sourceterms: Dict[str, List] = field(default_factory=dict)
    # tags used by documents that have no VersionSettings
    unknown_tags: List[str] = field(default_factory=list)

    def __bool__(self):
        return bool(self.untagged or self.sourceterms or self.unknown_tags)


def _ids_in_version(doc_cls, version: str) -> set:
    return set(doc_cls._get_collection().distinct(
        'original_id', {'version_tags': version}))


def _ref_id(ref):
    """ The id stored by a reference field, as a DBRef or a bare id """
    return getattr(ref, 'id', ref)


def find_orphans() -> Orphans:
    """ Find documents that no version can reach or that reference missing
    documents
    """
    result = Orphans()
    for cls in VERSIONED_CLASSES:
        coll = cls._get_collection()
        ids = coll.distinct('_id', {'version_tags': {'$in': [None, []]}})
        if ids:
            result.untagged[cls.__name__] = ids

    known = set(VersionSettings.objects.distinct('version_tag'))
    used = set()
    for cls in VERSIONED_CLASSES:
        used.update(cls._get_collection().distinct('version_tags'))
    # an empty array counts as null for distinct
    result.unknown_tags = sorted(used - known - {None})

    stcoll = SourceTerm._get_collection()
    for version in sorted(known):
        components = _ids_in_version(Component, version)
        specs = _ids_in_version(EmissionSpec, version)
        bad = []
        for st in stcoll.find({'version_tags': version},
                              {'assemblyRoot': 1, 'spec': 1}):
            root, spec = _ref_id(st.get('assemblyRoot')), _ref_id(st.get('spec'))
            if (root is None or root not in components
                    or (spec is not None and spec not in specs)):
                bad.append(st['_id'])
        if bad:
            result.sourceterms[version] = bad
    return result


def delete_orphans(orphans: Orphans = None) -> int:
    """ Remove the documents found by `find_orphans`: untagged documents are
    deleted, unknown tags are removed from all documents, and orphaned
    SourceTerms are removed from their version. Returns the number of
    documents changed or deleted
    """
    orphans = orphans if orphans is not None else find_orphans()
    changed = 0
    for version, ids in orphans.sourceterms.items():
        coll = SourceTerm._get_collection()
        changed += coll.update_many({'_id': {'$in': ids}},
                                    {'$pull': {'version_tags': version}}
                                    ).modified_count
    for cls in VERSIONED_CLASSES:
        coll = cls._get_collection()
        if orphans.unknown_tags:
            changed += coll.update_many(
                {'version_tags': {'$in': orphans.unknown_tags}},
                {'$pull': {'version_tags': {'$in': orphans.unknown_tags}}}
                ).modified_count
        # anything left without tags, including what was just untagged
        changed += coll.delete_many(
            {'version_tags': {'$in': [None, []]}}).deleted_count
    clear_calculation_cache()
    return changed


def clear_calculation_cache() -> None:
    """ Drop all stored and in-memory calculation results """
    CalculatedResults.drop_collection()
    clear_results_cache()


def rebuild_sourceterms(version: str) -> int:
    """ Delete the SourceTerms of a (writable) version and regenerate them
    from its components. Returns the new number of SourceTerms
    """
    check_writable(version)
    log.warning(f"Rebuilding SourceTerms for version '{version}'")
    SourceTerm.select_version(version).delete()
    for component in Component.select_version(version):
        if not isinstance(component, Assembly):
            signals.update_component(sender=None, document=component)
    touch(version)
    return SourceTerm.select_version(version).count()
