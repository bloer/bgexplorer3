""" The results of each SourceTerm of a component, with the keys to group them
by, as a `TermTable` to sum over. Values are in the version's display units
(see `termcalc`).
"""
from typing import List, Optional, Tuple
from ..component import Component, Assembly
from ..hiteff import HitEfficiency
from ..sourceterm import (ResultsCache, SourceTerm, find_sourceterms,
                          load_hiteffs, _cache_key, _cached, _ref_id)
from ..settings import get_settings, get_calc_token
from ..termcalc import TermCalculator
from .termtable import TermRecord, TermTable

# the records of single SourceTerms, by (id, stamp, calc token). The stamp
# changes when the SourceTerm does, and the version's calc token when
# anything else that the values depend on does (such as display units), so
# entries are never stale and the terms that didn't change are reused after an
# edit. Their component keys are the full placement paths.
_term_cache = ResultsCache(60000)
# loading and calculating this many SourceTerms at once
_LOAD_CHUNK = 2000


def available_scalars(version: str) -> List[str]:
    """ Scalar and ROI result names in `version`, displayed ones first """
    config = get_settings(version).hiteffdbconfig
    names = [k for k, v in config.display_scalars.items() if not v.hide]
    names += [roi.key for roi in config.rois]
    names += sorted(HitEfficiency.select_version(version)
                    .distinct('scalars_keys'))
    hidden = {k for k, v in config.display_scalars.items() if v.hide}
    return [n for n in dict.fromkeys(names) if n not in hidden]


def _component_path(placement_ids, child_ids: set) -> Tuple[str, ...]:
    """ The placement ids (as strings) in a SourceTerm's path below the object
    whose children's placement ids are `child_ids`. Empty for the object's own
    sources
    """
    for i, pid in enumerate(placement_ids):
        if pid in child_ids:
            return tuple(placement_ids[i:])
    return ()


def _exclude_large(query):
    # these are large and not needed here
    return query.exclude('assemblyPath', 'livetimes', 'assemblyPathStr')


def _record(sourceterm, calculator, spectra: bool) -> TermRecord:
    rate, blocks = calculator.hit_blocks(sourceterm, spectra)
    return TermRecord(sourceterm.id, sourceterm.group_keys(),
                      sourceterm.confidence_rating, tuple(rate),
                      tuple(block for block in blocks if block.values))


def _chunks(ids: list):
    for i in range(0, len(ids), _LOAD_CHUNK):
        yield ids[i:i + _LOAD_CHUNK]


def _scalar_records(query, config, token) -> List[TermRecord]:
    """ The record of each SourceTerm in `query`, in order, loading and
    calculating only those that aren't in the cache, a chunk at a time
    """
    stored = [(doc['_id'], doc.get('stamp')) for doc in
              query.only('stamp').as_pymongo()]
    cached = _term_cache.get_many((_id, stamp, token)
                                  for _id, stamp in stored if stamp)
    missing = [_id for _id, stamp in stored
               if not stamp or (_id, stamp, token) not in cached]
    query = _exclude_large(query)
    records = {}
    for ids in _chunks(missing):
        sourceterms = list(query(id__in=ids))
        calculator = TermCalculator(config,
                                    load_hiteffs(sourceterms, spectra=False))
        new = {}
        for st in sourceterms:
            stamp = st.stamp
            if not stamp:
                # saved before SourceTerms had stamps
                stamp = st.content_stamp()
                SourceTerm.objects(id=st.id).update(set__stamp=stamp)
            record = _record(st, calculator, False)
            records[st.id] = record
            new[st.id, stamp, token] = record
        _term_cache.put_many(new)
    result = []
    for _id, stamp in stored:
        record = cached.get((_id, stamp, token)) or records.get(_id)
        # a SourceTerm deleted since the ids were read is left out
        if record is not None:
            result.append(record)
    return result


def _spectrum_records(query, config):
    """ The records of the SourceTerms in `query`, a chunk at a time """
    ids = [doc['_id'] for doc in query.only('id').as_pymongo()]
    query = _exclude_large(query)
    for chunk in _chunks(ids):
        sourceterms = list(query(id__in=chunk))
        calculator = TermCalculator(config,
                                    load_hiteffs(sourceterms, spectra=True))
        for st in sourceterms:
            yield _record(st, calculator, True)


def term_table(obj: Component, relativeto: Optional[Assembly] = None,
               spectra: bool = False) -> TermTable:
    """ The scalar values (or if `spectra`, the spectra) of each SourceTerm
    of `obj` (optionally only as placed in `relativeto`) that has any, in the
    version's display units. Kept in memory until any data in the version
    changes, and shared, so must not be modified. When it does, the
    SourceTerms that didn't change are reused. Raises UnitMismatchError if
    saved data has units that don't fit together.
    """
    version = obj.active_version

    def calculate():
        config = get_settings(version).hiteffdbconfig
        query = find_sourceterms(obj, relativeto)
        if spectra:
            records = _spectrum_records(query, config)
        else:
            records = _scalar_records(query, config, get_calc_token(version))
        child_ids = {str(p.id) for p in getattr(obj, 'children', None) or []}
        return TermTable(records,
                         lambda path: _component_path(path, child_ids))
    key = _cache_key(obj, 'spectra' if spectra else 'terms', _ref_id(obj),
                     relativeto and _ref_id(relativeto))
    return _cached(key, calculate)
