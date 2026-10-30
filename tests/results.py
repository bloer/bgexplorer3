""" A reference calculation of results for the tests: the sum over
SourceTerms of each HitEfficiency's values times the SourceTerm's emission
rate, without the caching or batching of `bgexplorer.models.budget`
"""
from dataclasses import dataclass, field
from typing import Iterable, Optional
from bgexplorer.models.asymmetric import sum_values
from bgexplorer.models.common import multnone
from bgexplorer.models.sourceterm import find_sourceterms, load_hiteffs
from bgexplorer.models.verdoc import ref_id


@dataclass
class Results:
    scalars: dict = field(default_factory=dict)
    spectra: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)


def from_sourceterms(sourceterms: Iterable, spectra: bool = True
                     ) -> Optional[Results]:
    """ The summed results of `sourceterms`, or None if none have hiteffs """
    sourceterms = list(sourceterms)
    hiteffs = load_hiteffs(sourceterms, spectra)
    scalar_parts, hist_parts = {}, {}
    found = False
    for st in sourceterms:
        for ref in st._data.get('hiteffs') or []:
            hiteff = hiteffs.get(ref_id(ref), ref)
            found = True
            rate = st.emissionrate
            for key, val in hiteff.all_scalars.items():
                if val is not None:
                    scalar_parts.setdefault(key, []).append(
                        multnone(val, rate))
            if spectra:
                for key, val in hiteff.spectra.items():
                    if val is not None:
                        hist_parts.setdefault(key, []).append(
                            multnone(val, rate))
    if not found:
        return None
    result = Results(
        scalars={k: sum_values(v) for k, v in scalar_parts.items()},
        spectra={k: sum_values(v) for k, v in hist_parts.items()},
        sources=sourceterms)
    for value in result.scalars.values():
        value.ito_reduced_units()
    for value in result.spectra.values():
        value.hist.ito_reduced_units()
    return result


def for_object(obj, relativeto=None, spectra: bool = True
               ) -> Optional[Results]:
    """ The results for all SourceTerms of `obj`, optionally only those in
    the assembly `relativeto`
    """
    return from_sourceterms(find_sourceterms(obj, relativeto), spectra)
