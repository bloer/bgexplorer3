""" tracemalloc pass on the CDMS model: for each step, the peak, and the
allocations live at the highest point seen, by line and by call stack """
import contextlib
import gc
import sys
import threading
import time
import tracemalloc
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
import profile_standard as ps

URI = 'mongodb://127.0.0.1:27017/bexplorer3_testing_cdms'
TAG = 'examples-cdms7tower'
MB = ps.MB


def root_of(tag):
    from bgexplorer.models.component import Assembly
    return Assembly.select_version(tag).get(name='Total')


def show(snap, title):
    print(f"  -- {title}: by line", flush=True)
    for s in snap.statistics('lineno')[:12]:
        f = s.traceback[0]
        print(f"    {s.size / MB:8.1f} MB {s.count:9d} blocks  "
              f"{f.filename.split('site-packages/')[-1].split('src/')[-1]}:{f.lineno}",
              flush=True)
    print(f"  -- {title}: by stack (top 6, innermost 5 frames)", flush=True)
    for s in snap.statistics('traceback')[:6]:
        print(f"    {s.size / MB:8.1f} MB {s.count:9d} blocks", flush=True)
        for fr in s.traceback.format(limit=5)[-10:]:
            print("      " + fr.strip().replace('/home/agent/workspace/.claude/worktrees/background-jobs/', ''), flush=True)


class TMeter(ps.Meter):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        tracemalloc.stop()
        tracemalloc.start(12)

    @contextlib.contextmanager
    def step(self, op, label, profile=True):
        best = {'cur': 0, 'snap': None}
        halt = threading.Event()
        base = [0]

        def watch():
            while not halt.wait(0.2):
                cur = tracemalloc.get_traced_memory()[0]
                if cur - base[0] > 30 * MB and cur > best['cur'] * 1.15:
                    best['cur'] = cur
                    best['snap'] = tracemalloc.take_snapshot()

        gc.collect()
        base[0] = tracemalloc.get_traced_memory()[0]
        t = threading.Thread(target=watch, daemon=True)
        t.start()
        try:
            with super().step(op, label, profile=False):
                yield
        finally:
            halt.set()
            t.join()
            peak = self.results[-1].get('traced_peak_mb', 0)
            if best['snap'] is not None and peak > 100:
                show(best['snap'], f"{op}/{label} (snapshot at {(best['cur'] - base[0]) / MB:.0f} MB above start)")


ps.root_of = root_of
from bgexplorer.application.app import create_app
create_app(config={'MONGODB_URI': URI, 'TESTING': True})
meter = TMeter(None, 'cumulative', 25, True)
try:
    for name in sys.argv[1].split(','):
        ps.run(meter, TAG, name, False, True)
finally:
    meter.close()
