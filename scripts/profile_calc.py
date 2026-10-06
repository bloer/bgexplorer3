""" Time and profile background calculations on the qis example model.

This is a manual benchmark, not part of the test suite. Populating the
example is slow, so it is only done if the version doesn't exist yet or
--populate is given. All collections in the database are dropped when
populating from scratch with --clean!

Usage:
    python scripts/profile_calc.py [--uri URI] [--populate] [--clean]
                                   [--profile] [--sort cumulative]
"""
import argparse
import cProfile
import pstats
import time
import flask
import mongoengine
import sys
from pathlib import Path

VERSION = 'examples-qis'


def timeit(label, func, profile=None):
    start = time.perf_counter()
    if profile is not None:
        profile.enable()
    result = func()
    if profile is not None:
        profile.disable()
    print(f"{label:<40s} {time.perf_counter() - start:8.3f} s")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--uri', default='mongodb://127.0.0.1:27017/'
                        'bgexplorer3_profile')
    parser.add_argument('--populate', action='store_true',
                        help="(re)populate the example even if it exists")
    parser.add_argument('--clean', action='store_true',
                        help="drop the whole database before populating")
    parser.add_argument('--profile', action='store_true',
                        help="print cProfile stats of the cold page view")
    parser.add_argument('--sort', default='cumulative')
    parser.add_argument('--limit', type=int, default=40)
    parser.add_argument('--output', help="save the profile stats to a file")
    args = parser.parse_args()

    from bgexplorer.application.app import create_app
    from bgexplorer.models.component import Assembly
    from bgexplorer.models.sourceterm import CalculatedResults, SourceTerm
    from bgexplorer.models.settings import VersionSettings

    if args.clean:
        conn = mongoengine.connect(host=args.uri)
        conn.drop_database(mongoengine.get_db().name)
        mongoengine.disconnect()

    app = create_app(config={'MONGODB_URI': args.uri, 'TESTING': True})
    if args.populate or not VersionSettings.objects(version_tag=VERSION):
        # examples are not installed; load from the repo checkout
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                               / 'examples' / 'qis'))
        import qis
        timeit("populate_example",
               lambda: qis.populate_example(version_tag=VERSION, clean=True))

    root = (Assembly.select_version(VERSION).order_by('-hierarchy_level')
            .first())
    nst = SourceTerm.select_version(VERSION).count()
    print(f"root assembly '{root.name}', {nst} sourceterms")

    # start from an empty results cache
    CalculatedResults.objects.delete()
    timeit("for_object(root) cold, no save",
           lambda: CalculatedResults.for_object(root, save=False))

    with app.test_request_context():
        flask.g.active_version = VERSION
        url = flask.url_for('component.view', object=root)
    client = app.test_client()
    CalculatedResults.objects.delete()
    profile = cProfile.Profile() if args.profile else None
    response = timeit("GET root view cold", lambda: client.get(url), profile)
    assert response.status_code == 200, response.status_code
    timeit("GET root view warm", lambda: client.get(url))

    if profile is not None:
        if args.output:
            profile.dump_stats(args.output)
        stats = pstats.Stats(profile)
        stats.sort_stats(args.sort).print_stats(args.limit)


if __name__ == '__main__':
    main()
