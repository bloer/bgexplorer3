""" Generate the "standard" scenario that the profiling scripts use: a
deterministic model of realistic size.

By default:
    200 assays, each of U238, Th232, K40 and Co60, a mix of values and limits
    400 leaf components, one assay each, in an assembly hierarchy 5 levels deep
        (leaves are level 1, the root is level 5); some components and
        assemblies are placed several times, with different weights
    2000 HitEfficiencies (6 sources x 334 locations, truncated), each with 3
        spectra
    2 ROIs of each spectrum in the version's settings
The default addsources are kept, so U235 and Ra226 are generated from U238.

Usage:
    python scripts/standard_scenario.py [--uri URI] [--version TAG] [--clean]
                                        [--seed N]
All collections in the database are dropped with --clean!
"""
import argparse
import logging
import math
import random
import time
from typing import Callable, Dict, Optional

import numpy as np

SOURCES = ('U238', 'Th232', 'K40', 'Co60')
# the sources the hit efficiencies are simulated for; U235 and Ra226 are
# generated from U238
HITEFF_SOURCES = SOURCES + ('U235', 'Ra226')
MATERIALS = ('copper', 'steel', 'aluminum', 'PTFE', 'titanium', 'lead',
             'PEEK', 'kapton')
SPECTRA = ('low', 'mid', 'high')
# (start, stop) keV of the 2 ROIs of each spectrum
ROI_RANGES = ((0, 100), (100, 500))
WEIGHTS = (1, 1, 1, 1, 1, 1, 2, 2, 3, 4, 5, 10)
BIN_RANGE_KEV = 3000.
log = logging.getLogger(__name__)


def _say(message: str) -> None:
    print(message, flush=True)


def _pick_weight(rng: random.Random) -> float:
    return float(rng.choice(WEIGHTS))


def make_assay(index: int, rng: random.Random, version_tag: str,
               limit_fraction: float = .35):
    """ An assay of every one of SOURCES, some of them upper limits """
    from bgexplorer.models.assay import Assay
    sources = {}
    for name in SOURCES:
        center = {'U238': 10., 'Th232': 5., 'K40': 50., 'Co60': .5}[name]
        value = center * math.exp(rng.gauss(0, 1.5))
        if rng.random() < limit_fraction:
            sources[name] = f"< {value:.3g} mBq/kg"
        else:
            sources[name] = f"{value:.3g} ± {value * rng.uniform(.1, .5):.2g} mBq/kg"
    return Assay(name=f"assay-{index:03d}", version_tag=version_tag,
                 sources=sources)


def make_hiteff(source: str, location: str, rng: np.random.Generator,
                n_bins: int, version_tag: str):
    from bgexplorer.models.asymmetric import AsymmetricUncertainty
    from bgexplorer.models.common import units
    from bgexplorer.models.hiteff import HitEfficiency
    from bgexplorer.models.histogram import Histogram
    edges = np.linspace(0, BIN_RANGE_KEV, n_bins + 1) * units.keV
    centers = np.linspace(0, 1, n_bins, endpoint=False)
    spectra = {}
    for i, name in enumerate(SPECTRA):
        shape = np.exp(-centers * (2 + i)) * 40 * rng.uniform(.2, 3)
        counts = rng.poisson(shape * rng.uniform(.5, 2, n_bins))
        spectra[name] = Histogram(
            AsymmetricUncertainty.fromcounts(counts) * units('dru/mBq'),
            edges)
    return HitEfficiency(source=source, location=location, spectra=spectra,
                         version_tag=version_tag)


def make_rois() -> list:
    from bgexplorer.models.common import units
    from bgexplorer.models.settings import SpectrumROI
    return [SpectrumROI(spectrum=name, start=start * units.keV,
                        stop=stop * units.keV, mode='average')
            for name in SPECTRA for start, stop in ROI_RANGES]


def _build_tier(children: list, n_parents: int, level: int, rng: random.Random,
                version_tag: str, share_fraction: float,
                name: Optional[str] = None) -> list:
    """ Place each of `children` in one of `n_parents` assemblies, in order,
    and some of them in another, with a different weight, then save and return
    the assemblies, named `name` or by their `level`
    """
    from bgexplorer.models.component import Assembly, Placement
    placements = [[] for _ in range(n_parents)]
    for i, child in enumerate(children):
        placements[i * n_parents // len(children)].append(
            Placement(component=child, weight=_pick_weight(rng)))
    shared = rng.sample(children, round(share_fraction * len(children)))
    for child in shared:
        placements[rng.randrange(n_parents)].append(
            Placement(component=child, weight=_pick_weight(rng),
                      label=f"{child.name} (extra)"))
    assemblies = []
    for j, children_ in enumerate(placements):
        assemblies.append(Assembly(
            name=name or f"asm-L{level}-{j:03d}", version_tag=version_tag,
            children=children_).save())
    return assemblies


def build_standard_scenario(version_tag: str = 'standard', seed: int = 1,
                            n_assays: int = 200, n_leaves: int = 400,
                            n_hiteffs: int = 2000, n_bins: int = 200,
                            depth: int = 5, share_fraction: float = .15,
                            say: Callable[[str], None] = _say) -> Dict:
    """ Create the version `version_tag`, which mustn't exist, and fill it.
    Returns a summary of what was made. The same arguments always make the
    same model. `share_fraction` of the components or assemblies of each level
    are placed an extra time. The tree is `depth` levels deep, including
    the leaves and the root assembly 'root'
    """
    from bgexplorer.models.component import Component
    from bgexplorer.models.settings import get_settings
    from bgexplorer.versioning import create_version
    rng = random.Random(seed)
    nprng = np.random.default_rng(seed)
    started = time.perf_counter()

    def lap(message):
        say(f"{time.perf_counter() - started:7.1f} s  {message}")

    create_version(version_tag, editable=True,
                   description="The standard scenario for profiling")
    settings = get_settings(version_tag)
    settings.hiteffdbconfig.rois = make_rois()
    settings.save()
    lap(f"settings with {len(settings.hiteffdbconfig.rois)} ROIs")

    # hit efficiencies first: saving one is much cheaper while no
    # SourceTerm exists to match
    n_locations = math.ceil(n_hiteffs / len(HITEFF_SOURCES))
    pairs = [(source, f"loc-{i:03d}") for i in range(n_locations)
             for source in HITEFF_SOURCES][:n_hiteffs]
    for i, (source, location) in enumerate(pairs, 1):
        make_hiteff(source, location, nprng, n_bins, version_tag).save()
        if i % 500 == 0:
            lap(f"{i} hit efficiencies")

    assays = [make_assay(i, rng, version_tag).save() for i in range(n_assays)]
    lap(f"{len(assays)} assays")

    order = [assays[i % n_assays] for i in range(n_leaves)]
    rng.shuffle(order)
    leaves = []
    for i, assay in enumerate(order):
        leaves.append(Component(
            name=f"leaf-{i:03d}", version_tag=version_tag,
            material=rng.choice(MATERIALS),
            mass=f"{math.exp(rng.gauss(math.log(.5), 1.2)):.4g} kg",
            location=f"loc-{i % n_locations:03d}", specs=[assay]).save())
        if (i + 1) % 100 == 0:
            lap(f"{i + 1} leaf components")

    fanout = math.ceil(n_leaves ** (1 / (depth - 1)))
    tier = leaves
    for level in range(2, depth):
        n_parents = max(1, math.ceil(len(tier) / fanout))
        tier = _build_tier(tier, n_parents, level, rng, version_tag,
                           share_fraction)
        lap(f"{len(tier)} assemblies at level {level}")
    root = _build_tier(tier, 1, depth, rng, version_tag, share_fraction,
                       name='root')[0]
    lap(f"root assembly, hierarchy level {root.hierarchy_level}")
    return dict(version_tag=version_tag, assays=len(assays),
                leaves=len(leaves), hiteffs=len(pairs), root=root.name,
                hierarchy_level=root.hierarchy_level,
                seconds=time.perf_counter() - started)


def summarize(version_tag: str) -> Dict[str, int]:
    """ Counts of what is in `version_tag` """
    from bgexplorer.models.assay import Assay
    from bgexplorer.models.component import Assembly, Component
    from bgexplorer.models.hiteff import HitEfficiency
    from bgexplorer.models.settings import get_settings
    from bgexplorer.models.sourceterm import SourceTerm
    assemblies = Assembly.select_version(version_tag)
    placements = sum(len(a.children) for a in assemblies)
    root = assemblies.order_by('-hierarchy_level').first()
    return dict(
        assays=Assay.select_version(version_tag).count(),
        leaves=(Component.select_version(version_tag).count()
                - assemblies.count()),
        assemblies=assemblies.count(), placements=placements,
        root_placements=len(root.children),
        hierarchy_level=root.hierarchy_level,
        hiteffs=HitEfficiency.select_version(version_tag).count(),
        rois=len(get_settings(version_tag).hiteffdbconfig.rois),
        sourceterms=SourceTerm.select_version(version_tag).count())


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--uri', default='mongodb://127.0.0.1:27017/'
                        'bgexplorer3_profile')
    parser.add_argument('--version', default='standard')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--clean', action='store_true',
                        help="drop the whole database first")
    parser.add_argument('--assays', type=int, default=200)
    parser.add_argument('--leaves', type=int, default=400)
    parser.add_argument('--hiteffs', type=int, default=2000)
    parser.add_argument('--bins', type=int, default=200)
    args = parser.parse_args()

    import mongoengine
    from bgexplorer.application.app import create_app
    if args.clean:
        conn = mongoengine.connect(host=args.uri)
        conn.drop_database(mongoengine.get_db().name)
        mongoengine.disconnect()
    create_app(config={'MONGODB_URI': args.uri, 'TESTING': True})
    result = build_standard_scenario(args.version, args.seed, args.assays,
                                     args.leaves, args.hiteffs, args.bins)
    _say(f"made {result}")
    _say(f"counts {summarize(args.version)}")


if __name__ == '__main__':
    main()
