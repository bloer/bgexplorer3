""" Time, measure the memory of, and profile the worst-case operations on the
standard scenario (see standard_scenario.py).

Each operation runs on a fresh branch of the scenario's version, which is
deleted again afterwards, so every one starts from the same data. Unless
--no-view, the root's budget is calculated first, as it would be when someone
edits a model they have just looked at, and again afterwards, to measure what
the edit costs the next view. The in-memory caches are emptied before each
operation. Memory is the resident set size of this process, sampled every few
milliseconds, so its peak is the real one, with --tracemalloc also the peak of
what Python allocated (which makes everything slower).

With --jobs-mode thread or process, the operations that edit (the change,
and the save or rebuild it needs) are run as a background job by a pool of
--workers threads or processes, as the web application would, and the step
"job" is the wall clock from submitting the job until it has finished. The
views before and after stay in this process. The job reports its own steps
and the peak resident size of the process that ran it, which in process mode
is a worker, not this process, and which starts with a cold cache.

This is a manual benchmark, not part of the test suite.

Usage:
    python scripts/profile_standard.py [--uri URI] [--version TAG] [--populate]
        [--clean] [--ops view,add_roi,...] [--in-place] [--no-view]
        [--profile DIR] [--sort cumulative] [--limit 25] [--tracemalloc]
        [--json FILE] [--jobs-mode inline|thread|process] [--workers N]
"""
import argparse
import contextlib
import cProfile
import gc
import importlib
import io
import json
import os
import pstats
import sys
import threading
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MB = 2 ** 20
PAGE = os.sysconf('SC_PAGE_SIZE')


class Meter:
    """ Measures steps by their duration and memory, and keeps the results """
    def __init__(self, profile_dir=None, sort='cumulative', limit=25,
                 trace=False, interval=.005):
        self.profile_dir = profile_dir
        self.sort = sort
        self.limit = limit
        self.trace = trace
        self.results = []
        self._interval = interval
        self._peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        if trace:
            tracemalloc.start()

    @staticmethod
    def rss() -> int:
        with open('/proc/self/statm') as f:
            return int(f.read().split()[1]) * PAGE

    def _sample(self):
        while not self._stop.is_set():
            self._peak = max(self._peak, self.rss())
            time.sleep(self._interval)

    def close(self):
        self._stop.set()
        self._thread.join()

    @contextlib.contextmanager
    def step(self, op: str, label: str, profile: bool = True):
        """ Time, measure and print the block, under the name `label`; also
        profile it if requested
        """
        from bgexplorer.models.sourceterm import ResultsCache
        gc.collect()
        start_rss = self._peak = self.rss()
        if self.trace:
            tracemalloc.reset_peak()
            start_traced = tracemalloc.get_traced_memory()[0]
        profiler = cProfile.Profile() if self.profile_dir and profile else None
        started = time.perf_counter()
        if profiler:
            profiler.enable()
        try:
            yield
        finally:
            if profiler:
                profiler.disable()
            seconds = time.perf_counter() - started
            self._peak = max(self._peak, self.rss())
            end_rss = self.rss()
            result = dict(op=op, step=label, seconds=seconds,
                          rss_start_mb=start_rss / MB,
                          rss_peak_mb=self._peak / MB,
                          rss_end_mb=end_rss / MB,
                          caches={c.name: [len(c), c.nbytes / MB]
                                  for c in ResultsCache.instances
                                  if c.name and len(c)})
            line = (f"{op + ' / ' + label:<40s} {seconds:8.2f} s   "
                    f"peak +{(self._peak - start_rss) / MB:7.0f} MB   "
                    f"end {end_rss / MB:7.0f} MB (+{(end_rss - start_rss) / MB:.0f})")
            if self.trace:
                traced, peak = tracemalloc.get_traced_memory()
                result['traced_peak_mb'] = (peak - start_traced) / MB
                line += f"   traced peak +{result['traced_peak_mb']:.0f} MB"
            self.results.append(result)
            print(line, flush=True)
            if result['caches']:
                print("    caches: " + ", ".join(
                    f"{name} {n} ({size:.0f} MB)"
                    for name, (n, size) in result['caches'].items()),
                    flush=True)
            if profiler:
                self._report(profiler, op, label)

    def _report(self, profiler, op, label):
        directory = Path(self.profile_dir)
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{op}.{label}".replace(' ', '_').replace('/', '_')
        profiler.dump_stats(str(directory / f"{name}.prof"))
        out = io.StringIO()
        pstats.Stats(profiler, stream=out).sort_stats(self.sort)\
            .print_stats(self.limit)
        text = out.getvalue()
        (directory / f"{name}.txt").write_text(text)
        print(text, flush=True)


def root_of(tag: str):
    from bgexplorer.models.component import Assembly
    return Assembly.select_version(tag).get(name='root')


def view(meter: Meter, op: str, tag: str, label: str = 'view',
         spectra: bool = False, profile: bool = True):
    """ Calculate the root's contributions table and dashboard (or, if
    `spectra`, its table of spectra), as its page would
    """
    from bgexplorer.models.budget.dashboard import dashboard
    from bgexplorer.models.budget.table import table, table_columns
    from bgexplorer.models.budget.terms import term_table
    root = root_of(tag)
    if spectra:
        with meter.step(op, f"{label} spectra", profile):
            tt = term_table(root, spectra=True)
        return
    scalar = table_columns(tag)[0][0]
    with meter.step(op, f"{label} term_table", profile):
        tt = term_table(root)
    with meter.step(op, f"{label} table", profile):
        table(root)
    with meter.step(op, f"{label} dashboard", profile):
        dashboard(root, scalar)


# The operations. Each takes (meter, op, tag) and makes the change
# measured, with `meter.step` around each step.

def op_view(meter, op, tag):
    pass


def op_spectra(meter, op, tag):
    view(meter, op, tag, 'cold', spectra=True)


def op_add_roi(meter, op, tag):
    """ A new ROI: evaluated for all hit efficiencies with its spectrum """
    from bgexplorer.models.common import units
    from bgexplorer.models.settings import SpectrumROI, get_settings
    settings = get_settings(tag)
    settings.hiteffdbconfig.rois.append(
        SpectrumROI(spectrum='mid', start=500 * units.keV,
                    stop=1500 * units.keV, mode='average'))
    with meter.step(op, 'save settings'):
        settings.save()


def op_root_weights(meter, op, tag):
    """ Change the weight of every placement in the root, which changes every
    SourceTerm below it
    """
    root = root_of(tag)
    for placement in root.children:
        placement.weight = placement.weight * 2
    with meter.step(op, f"save root ({len(root.children)} placements)"):
        root.save()


def op_add_leaf(meter, op, tag):
    """ A new component placed in an assembly that is placed several times """
    from bgexplorer.models.assay import Assay
    from bgexplorer.models.component import Assembly, Component, Placement
    parent = next(a for a in Assembly.select_version(tag)(hierarchy_level=2)
                  if a.find_parents().count() > 1)
    assay = Assay.select_version(tag).first()
    leaf = Component(name='new-leaf', version_tag=tag, material='copper',
                     mass='1 kg', location='loc-000', specs=[assay])
    with meter.step(op, 'save leaf'):
        leaf.save()
    parent.children.append(Placement(component=leaf, weight=2))
    with meter.step(op, f"place in {parent.name}"):
        parent.save()


def op_edit_assay(meter, op, tag):
    """ Change the rate of an assay that components use """
    from bgexplorer.models.assay import Assay
    assay = Assay.select_version(tag).first()
    for source in assay.sources:
        if source.name == 'U238':
            source.rate = '123 mBq/kg'
    with meter.step(op, 'save assay'):
        assay.save()


def op_rebuild(meter, op, tag):
    """ Regenerate every SourceTerm from the components """
    from bgexplorer.models.signals import rebuild_sourceterms
    with meter.step(op, 'rebuild_sourceterms'):
        rebuild_sourceterms(tag)


def op_export(meter, op, tag):
    """ Export the whole version to a file """
    from bgexplorer.versioning.archive import export_version
    buffer = io.BytesIO()
    with meter.step(op, 'export_version'):
        export_version(tag, buffer)
    print(f"    exported {buffer.tell() / MB:.1f} MB", flush=True)


# name: (function, calculate the budget before and after it)
OPS = dict(view=(op_view, False), spectra=(op_spectra, False),
           add_roi=(op_add_roi, True), root_weights=(op_root_weights, True),
           add_leaf=(op_add_leaf, True), edit_assay=(op_edit_assay, True),
           rebuild=(op_rebuild, True), export=(op_export, False))


BENCH_KIND = 'bench_op'
JOB_TIMEOUT = 6 * 3600
PROBE = False
PROBE_PERIOD = 0.25


class Probe:
    """ Requests the table and dashboard of a version, as its pages would, from
    a thread of this process while a job runs, to see what the job costs the
    pages: the views are of the results as they were before the edit """

    def __init__(self, tag: str):
        self.tag = tag
        self.latencies = []
        self.errors = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        from bgexplorer.models.budget.dashboard import dashboard
        from bgexplorer.models.budget.table import table, table_columns
        while not self._stop.wait(PROBE_PERIOD):
            started = time.perf_counter()
            try:
                root = root_of(self.tag)
                table(root)
                dashboard(root, table_columns(self.tag)[0][0])
            except Exception:
                self.errors += 1
                continue
            self.latencies.append(time.perf_counter() - started)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()

    def summary(self) -> dict:
        lat = sorted(self.latencies)
        if not lat:
            return {'requests': 0, 'errors': self.errors}
        return {'requests': len(lat), 'errors': self.errors,
                'median_s': lat[len(lat) // 2], 'p95_s': lat[int(len(lat) * .95)],
                'max_s': lat[-1]}


def register_bench_kind():
    """ Register the job kind that runs one of the operations above """
    from bgexplorer import jobs
    from bgexplorer.models.sourceterm import clear_results_cache

    @jobs.job_kind(BENCH_KIND, label="Benchmark operation")
    def bench_op(params):
        if params['cold']:
            clear_results_cache()
        meter = Meter()
        try:
            OPS[params['op']][0](meter, params['op'], params['tag'])
        finally:
            meter.close()
        return {'steps': meter.results, 'pid': os.getpid(),
                'peak_rss_mb': max([r['rss_peak_mb'] for r in meter.results],
                                   default=0)}


def bench_init(uri: str, adapter: str = None):
    """ Initializer of the worker processes: the application, and the kind.
    `adapter` is 'module:function', called to adapt the operations as the
    main program did (see profile_cdms.py) """
    from bgexplorer.application.jobs import worker_init
    worker_init(uri)
    register_bench_kind()
    if adapter:
        module, function = adapter.split(':')
        getattr(importlib.import_module(module), function)()


def make_runner(mode: str, workers: int, uri: str, adapter: str = None):
    """ A started job runner for `op_as_job`; `mode` is 'thread' or 'process' """
    from bgexplorer import jobs
    register_bench_kind()
    runner = jobs.JobRunner(
        mode=mode, workers=workers, poll_seconds=0.05,
        initializer=bench_init if mode == 'process' else None,
        initargs=(uri, adapter))
    runner.start()
    return runner


def op_as_job(meter: Meter, runner, name: str, tag: str):
    """ Make the change of the operation `name` in a job, and wait for it """
    cold = runner.mode == 'process'
    params = {'op': name, 'tag': tag, 'cold': cold}
    probe = Probe(tag) if PROBE else contextlib.nullcontext()
    with probe, meter.step(name, f"job ({runner.mode})", profile=False):
        job = runner.wait(runner.submit(BENCH_KIND, params, scope=tag),
                          JOB_TIMEOUT)
    if PROBE:
        summary = probe.summary()
        print(f"    views of the version during the job: {summary}",
              flush=True)
        meter.results[-1]['probe'] = summary
    if job is None or job.status != 'succeeded':
        raise RuntimeError(f"The job of {name} didn't succeed: "
                           f"{job and (job.status, job.error)}")
    result = job.result
    where = f"worker pid {result['pid']}" if cold else "this process"
    print(f"    job ran in {where}, peak {result['peak_rss_mb']:.0f} MB; "
          "its steps:", flush=True)
    for step in result['steps']:
        print(f"      {step['step']:<36s} {step['seconds']:8.2f} s   "
              f"peak {step['rss_peak_mb']:7.0f} MB", flush=True)
        meter.results.append(dict(step, worker=True, mode=runner.mode))
    meter.results[-len(result['steps']) - 1]['job_peak_rss_mb'] = \
        result['peak_rss_mb']


def run(meter: Meter, base: str, name: str, in_place: bool, with_view: bool,
        runner=None):
    from bgexplorer.models.sourceterm import clear_results_cache
    from bgexplorer.versioning import create_branch, delete_version
    func, edits = OPS[name]
    tag = base
    if not in_place:
        tag = f"{base}-op-{name}".replace('_', '-')
        with contextlib.suppress(KeyError):
            delete_version(tag)
        with meter.step(name, 'create branch', profile=False):
            create_branch(tag, base)
    clear_results_cache()
    try:
        if name == 'view' or (edits and with_view):
            view(meter, name, tag, 'before', profile=False)
        if runner is not None and edits:
            op_as_job(meter, runner, name, tag)
        else:
            func(meter, name, tag)
        if edits and with_view:
            view(meter, name, tag, 'after')
    finally:
        if not in_place:
            delete_version(tag)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--uri', default='mongodb://127.0.0.1:27017/'
                        'bgexplorer3_profile')
    parser.add_argument('--version', default='standard')
    parser.add_argument('--populate', action='store_true',
                        help="(re)generate the scenario even if it exists")
    parser.add_argument('--clean', action='store_true',
                        help="drop the whole database before populating")
    parser.add_argument('--ops', default=','.join(OPS),
                        help="comma-separated, from: " + ', '.join(OPS))
    parser.add_argument('--in-place', action='store_true',
                        help="run on the version itself, changing it, rather "
                        "than on a branch of it")
    parser.add_argument('--no-view', action='store_true',
                        help="don't calculate the budget before and after")
    parser.add_argument('--profile', metavar='DIR',
                        help="profile each step, saving to DIR and printing")
    parser.add_argument('--sort', default='cumulative')
    parser.add_argument('--limit', type=int, default=25)
    parser.add_argument('--tracemalloc', action='store_true')
    parser.add_argument('--json', metavar='FILE',
                        help="save the results to compare runs")
    parser.add_argument('--jobs-mode', choices=('inline', 'thread', 'process'),
                        default='inline',
                        help="run the operations that edit as background "
                        "jobs of a pool of threads or processes")
    parser.add_argument('--workers', type=int, default=4,
                        help="size of the pool with --jobs-mode")
    parser.add_argument('--probe', action='store_true',
                        help="with --jobs-mode, request the table and "
                        "dashboard from this process while each job runs and "
                        "report how long that takes")
    args = parser.parse_args()
    global PROBE
    PROBE = args.probe
    ops = [op for op in args.ops.split(',') if op]
    unknown = set(ops) - set(OPS)
    if unknown:
        parser.error(f"unknown ops: {', '.join(sorted(unknown))}")

    import mongoengine
    from bgexplorer.application.app import create_app
    from bgexplorer.versioning import version_exists
    from standard_scenario import build_standard_scenario, summarize
    if args.clean:
        conn = mongoengine.connect(host=args.uri)
        conn.drop_database(mongoengine.get_db().name)
        mongoengine.disconnect()
    create_app(config={'MONGODB_URI': args.uri, 'TESTING': True})
    if args.populate or not version_exists(args.version):
        if version_exists(args.version):
            from bgexplorer.versioning import delete_version
            delete_version(args.version)
        build_standard_scenario(args.version)
    print(f"scenario {args.version}: {summarize(args.version)}", flush=True)

    runner = None
    if args.jobs_mode != 'inline':
        runner = make_runner(args.jobs_mode, args.workers, args.uri)
    meter = Meter(args.profile, args.sort, args.limit, args.tracemalloc)
    try:
        for name in ops:
            run(meter, args.version, name, args.in_place, not args.no_view,
                runner)
    finally:
        meter.close()
        if runner is not None:
            runner.stop()
    if args.json:
        Path(args.json).write_text(json.dumps(meter.results, indent=1))


if __name__ == '__main__':
    main()
