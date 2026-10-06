#!/usr/bin/env python3

from bgexplorer import Histogram, HitEfficiency, AsymmetricUncertainty, units
import uproot
import numpy as np
import pint

def get_counts(rootfilename: str, treename: str, branchname: str = 'edep',
               bins: int | np.ndarray = 100) -> np.ndarray:
    """ Histogram counts from a root TTree branch.

    Parameters
    ----------
    rootfilename : str
        The path to the ROOT file.
    treename : str
        The name of the TTree within the ROOT file.
    branchname : str, optional
        The name of the branch to histogram, by default 'edep'.
    bins : int | np.ndarray, optional
        The number of bins or the bin edges, by default 100.

    Returns
    -------
    counts, bins : tuple[np.ndarray, np.ndarray]
        The histogram counts and the bin edges.
    """
    # initialize empty
    counts = None
    for chunk in uproot.iterate(f'{rootfilename}:{treename}', branchname,
                                library='np'):
        if counts is None:
            counts, bins = np.histogram(chunk[branchname], bins=bins)
        else:
            counts += np.histogram(chunk[branchname], bins=bins)[0]
    return counts, bins


def root_to_hiteff(rootfilenames: str, treename: str, nprimaries: int,
                   branchname: str = 'edep', bins: int | np.ndarray = 100,
                   binsunit: str | pint.Unit = 'MeV',
                   **kwargs):
    """ Create a HitEfficiency object from a ROOT TTree.
    The returned HitEfficiency has two spectra: counts/MeV/s/Bq and
    counts above threshold/s/Bq, as well as three scalars:
    counts/s/Bq, counts_above_1MeV/s/Bq, and dose [keV/s/Bq].
    The normalizations are such that multiplying by a total emission rate
    in Bq gives differential count rate units.

    Parameters
    ----------
    rootfilenames : str
        The path to the ROOT file(s).
    treename : str
        The name of the TTree within the ROOT file.
    nprimaries : int
        The number of primary particles.
    branchname : str, optional
        The name of the branch to histogram, by default 'edep'.
    bins : int | np.ndarray, optional
        The number of bins or the bin edges, by default 100.
    binsunit : str | pint.Unit, optional
        The unit of the bins, by default 'MeV'.
    **kwargs
        Additional keyword arguments passed to `HitEfficiency`
        Must contain 'source' and 'location'

    Returns
    -------
    HitEfficiency
        The resulting HitEfficiency object.
    """
    counts, bins = get_counts(rootfilenames, treename, branchname=branchname, bins=bins)
    counts_above_threshold = np.cumsum(counts[::-1])[::-1]
    bins = bins * units(binsunit)
    binwidths = bins[1:] - bins[:-1]

    u = units('1/s/Bq')
    counts = AsymmetricUncertainty.fromcounts(counts) / nprimaries / binwidths * u
    counts_above_threshold = AsymmetricUncertainty.fromcounts(counts_above_threshold) / nprimaries * u

    spectra = dict(
        counts = Histogram(counts, bins),
        counts_above_threshold = Histogram(counts_above_threshold, bins),
    )

    scalars = dict(
        counts = counts_above_threshold[0],
        counts_above_1MeV = spectra['counts_above_threshold'].val(1*units.MeV),
        dose = np.sum(counts * binwidths * bins[:-1])
    )

    hiteff = HitEfficiency(nprimaries=nprimaries, spectra=spectra, scalars=scalars,
                           **kwargs)
    return hiteff

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <rootfilenames> <treename> <nprimaries>")
        sys.exit(1)
    rootfilenames = sys.argv[1]
    treename = sys.argv[2]
    nprimaries = int(sys.argv[3])
    hiteff = root_to_hiteff(rootfilenames, treename, nprimaries, source='example', location='example')
    print(hiteff.to_json(indent=4))
    sys.exit(0)