""" The results of each SourceTerm of a component, with the keys to group them
by. Values are already in the version's display units (see `termcalc`).
"""
from typing import List, NamedTuple, Optional, Tuple
from bson import ObjectId
from ..component import Component, Assembly
from ..hiteff import HitEfficiency
from ..sourceterm import (ResultsCache, SourceTerm, find_sourceterms,
                          load_hiteffs, _cache_key, _cached, _ref_id)
from ..settings import get_settings, get_calc_token
from ..termcalc import TermCalculator


class Term(NamedTuple):
    """ One SourceTerm's value of each scalar (an AsymmetricUncertainty) or
    spectrum (a Histogram) it has, in display units, and its key for each
    groupby (see `SourceTerm.group_keys`). The component key is the path of
    placement ids (as strings) from the object being broken down
    """
    sourceterm_id: ObjectId
    values: dict
    keys: dict
    # the confidence rating of the component the SourceTerm belongs to
    rating: Optional[int] = None


# the Terms of single SourceTerms, by (id, stamp, calc token). The stamp
# changes when the SourceTerm does, and the version's calc token when
# anything else that the values depend on does (such as display units), so
# entries are never stale and the terms that didn't change are reused after an
# edit. Their component keys are the full placement paths.
_term_cache = ResultsCache(60000)
# loading this many SourceTerms at once by id
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


def _scalar_terms(query, config, token) -> List[Term]:
    """ The Term of each SourceTerm in `query`, in order, loading and
    calculating only those that aren't in the cache
    """
    stored = [(doc['_id'], doc.get('stamp')) for doc in
              query.only('stamp').as_pymongo()]
    cached = _term_cache.get_many((_id, stamp, token)
                                  for _id, stamp in stored if stamp)
    missing = [_id for _id, stamp in stored
               if not stamp or (_id, stamp, token) not in cached]
    query = _exclude_large(query)
    if len(missing) == len(stored):
        batches = [list(query)]
    else:
        batches = (list(query(id__in=missing[i:i + _LOAD_CHUNK]))
                   for i in range(0, len(missing), _LOAD_CHUNK))
    terms = {}
    for sourceterms in batches:
        calculator = TermCalculator(config,
                                    load_hiteffs(sourceterms, spectra=False))
        new = {}
        for st in sourceterms:
            stamp = st.stamp
            if not stamp:
                # saved before SourceTerms had stamps
                stamp = st.content_stamp()
                SourceTerm.objects(id=st.id).update(set__stamp=stamp)
            term = Term(st.id, calculator.scalars(st), st.group_keys(),
                        st.confidence_rating)
            terms[st.id] = term
            new[st.id, stamp, token] = term
        _term_cache.put_many(new)
    result = []
    for _id, stamp in stored:
        term = cached.get((_id, stamp, token)) or terms.get(_id)
        # a SourceTerm deleted since the ids were read is left out
        if term is not None:
            result.append(term)
    return result


def _spectrum_terms(query, config) -> List[Term]:
    sourceterms = list(_exclude_large(query))
    calculator = TermCalculator(config, load_hiteffs(sourceterms, spectra=True))
    return [Term(st.id, calculator.spectra(st), st.group_keys(),
                 st.confidence_rating) for st in sourceterms]


def term_values(obj: Component, relativeto: Optional[Assembly] = None,
                spectra: bool = False) -> List[Term]:
    """ The scalar values (or if `spectra`, the spectra) of each SourceTerm
    of `obj` (optionally only as placed in `relativeto`) that has any, in the
    version's display units. Kept in memory until any data in the version
    changes, and shared, so must not be modified. When it does, the scalars of
    the SourceTerms that didn't change are reused. Raises UnitMismatchError if
    saved data has units that don't fit together.
    """
    version = obj.active_version

    def calculate():
        config = get_settings(version).hiteffdbconfig
        query = find_sourceterms(obj, relativeto)
        if spectra:
            terms = _spectrum_terms(query, config)
        else:
            terms = _scalar_terms(query, config, get_calc_token(version))
        child_ids = {str(p.id) for p in getattr(obj, 'children', None) or []}
        return [term._replace(keys=dict(
                    term.keys, component=_component_path(
                        term.keys['component'], child_ids)))
                for term in terms if term.values]
    key = _cache_key(obj, 'spectra' if spectra else 'terms', _ref_id(obj),
                     relativeto and _ref_id(relativeto))
    return _cached(key, calculate)
