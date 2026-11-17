""" Run the profile_standard ops against the CDMS model, adapting the ones that
assume the standard scenario's structure. Usage: profile_cdms.py [--ops ...]
[--profile DIR] [--in-place] [--json F] [--jobs-mode inline|thread|process]
[--workers N]"""
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
import profile_standard as ps

URI = 'mongodb://127.0.0.1:27017/bexplorer3_testing_cdms'
TAG = 'examples-cdms7tower'


def root_of(tag):
    from bgexplorer.models.component import Assembly
    return Assembly.select_version(tag).get(name='Total')


def op_add_roi(meter, op, tag):
    from bgexplorer.models.common import units
    from bgexplorer.models.settings import SpectrumROI, get_settings
    settings = get_settings(tag)
    first = settings.hiteffdbconfig.rois[0]
    settings.hiteffdbconfig.rois.append(
        SpectrumROI(spectrum=first.spectrum, start=500 * units.keV,
                    stop=1500 * units.keV, mode='average'))
    with meter.step(op, 'save settings'):
        settings.save()


def op_add_leaf(meter, op, tag):
    from bgexplorer.models.assay import Assay
    from bgexplorer.models.component import Assembly, Component, Placement
    parent = Assembly.select_version(tag)(hierarchy_level=2).first()
    assay = Assay.select_version(tag).first()
    leaf = Component(name='new-leaf', version_tag=tag, material='copper',
                     mass='1 kg', specs=[assay])
    with meter.step(op, 'save leaf'):
        leaf.save()
    parent.children.append(Placement(component=leaf, weight=2))
    with meter.step(op, f"place in {parent.name}"):
        parent.save()


def op_edit_assay(meter, op, tag):
    """ The assay used by the most components, doubling its first source """
    from bgexplorer.models.assay import Assay
    from bgexplorer.models.component import Component
    best = max(Assay.select_version(tag),
               key=lambda a: Component.select_version(tag)(
                   spec_entries__spec=a).count())
    print(f"    assay {best.name}, used by "
          f"{Component.select_version(tag)(spec_entries__spec=best).count()}",
          flush=True)
    source = next(s for s in best.sources if not str(s.rate).startswith('<'))
    source.rate = source.rate * 2
    with meter.step(op, 'save assay'):
        best.save()


def adapt():
    """ Also called by the worker processes of the job modes """
    ps.root_of = root_of
    ps.OPS['add_roi'] = (op_add_roi, True)
    ps.OPS['add_leaf'] = (op_add_leaf, True)
    ps.OPS['edit_assay'] = (op_edit_assay, True)


def main():
    adapt()
    parser = argparse.ArgumentParser()
    parser.add_argument('--ops', default=','.join(ps.OPS))
    parser.add_argument('--in-place', action='store_true')
    parser.add_argument('--no-view', action='store_true')
    parser.add_argument('--profile')
    parser.add_argument('--limit', type=int, default=25)
    parser.add_argument('--json')
    parser.add_argument('--jobs-mode', choices=('inline', 'thread', 'process'),
                        default='inline')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    ps.PROBE = args.probe

    from bgexplorer.application.app import create_app
    create_app(config={'MONGODB_URI': URI, 'TESTING': True})
    runner = None
    if args.jobs_mode != 'inline':
        runner = ps.make_runner(args.jobs_mode, args.workers, URI,
                                'profile_cdms:adapt')
    meter = ps.Meter(args.profile, 'cumulative', args.limit)
    try:
        for name in args.ops.split(','):
            ps.run(meter, TAG, name, args.in_place, not args.no_view, runner)
    finally:
        meter.close()
        if runner is not None:
            runner.stop()
    if args.json:
        Path(args.json).write_text(json.dumps(meter.results, indent=1))


if __name__ == '__main__':
    main()
