""" What the cold spectra table of the CDMS root keeps in memory afterwards, by
kind, and the process memory before, at the peak and after. Usage:
retained_cdms.py [name]   (name: the spectrum whose columns to build)
"""
import gc
import sys
import threading
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'src'))
import numpy as np

URI = 'mongodb://127.0.0.1:27017/bexplorer3_testing_cdms'
TAG = 'examples-cdms7tower'
MB = 2**20
PAGE = 4096


def rss():
    with open('/proc/self/statm') as f:
        return int(f.read().split()[1]) * PAGE


def base(a):
    a = getattr(a, 'm', a)
    a = np.asarray(a)
    while isinstance(a.base, np.ndarray):
        a = a.base
    return a


class Sampler:
    def __init__(self):
        self.peak = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, rss())
            time.sleep(.01)

    def stop(self):
        self._stop.set()
        self._t.join()


def main():
    from bgexplorer.application.app import create_app
    create_app(config={'MONGODB_URI': URI, 'TESTING': True})
    from bgexplorer.models.component import Assembly
    from bgexplorer.models.budget.terms import term_table, _spectra_cache
    from bgexplorer.models.sourceterm import estimate_bytes
    root = Assembly.select_version(TAG).get(name='Total')
    gc.collect()
    start = rss()
    sampler = Sampler()
    t0 = time.perf_counter()
    tt = term_table(root, spectra=True)
    seconds = time.perf_counter() - t0
    sampler.stop()
    peak = sampler.peak
    gc.collect()
    after = rss()
    print(f"cold spectra: {seconds:.1f} s; RSS start {start / MB:.0f} MB, "
          f"peak +{(peak - start) / MB:.0f} MB, after gc +"
          f"{(after - start) / MB:.0f} MB")
    print(f"terms {len(tt)}, blocks {len(tt._blocks)}")

    names = {}
    moments_ids, edges_ids = {}, {}
    n_values = 0
    for block in tt._blocks:
        for key, hv in block.values.items():
            n_values += 1
            names[key] = names.get(key, 0) + 1
            for a in hv.moments:
                b = base(a)
                moments_ids[id(b)] = b.nbytes
            if hv.bin_edges is not None:
                b = base(hv.bin_edges)
                edges_ids[id(b)] = b.nbytes
    print(f"hit values {n_values} over {len(names)} names; "
          + ', '.join(f"{k}: {v}" for k, v in sorted(names.items())))
    print(f"blocks' moment arrays: {len(moments_ids)} unique, "
          f"{sum(moments_ids.values()) / MB:.0f} MB")
    print(f"blocks' bin edges: {len(edges_ids)} unique arrays, "
          f"{sum(edges_ids.values()) / MB:.0f} MB")
    if edges_ids:
        # how many distinct contents
        contents = set()
        for block in tt._blocks:
            for hv in block.values.values():
                if hv.bin_edges is not None:
                    contents.add(base(hv.bin_edges).tobytes())
        print(f"  distinct bin edge contents: {len(contents)}")
    print(f"tt.nbytes (cache estimate) {tt.nbytes / MB:.0f} MB "
          f"(blocks {tt._block_bytes / MB:.0f})")
    cache = _spectra_cache
    print(f"spectra cache max_bytes {cache.max_bytes / MB:.0f} MB; "
          f"kept entries: {len(cache._data) if hasattr(cache, '_data') else '?'}"
          f"; attrs {[a for a in vars(cache)][:12]}")

    name = sys.argv[1] if len(sys.argv) > 1 else sorted(names)[0]
    before = rss()
    cols = tt.columns(name)
    gc.collect()
    print(f"columns({name!r}): {len(cols.keys)} columns, "
          f"{cols.nbytes / MB:.0f} MB; RSS +{(rss() - before) / MB:.0f} MB")
    t0 = time.perf_counter()
    tt.total_spectrum(name, np.flatnonzero(tt.having(name)))
    print(f"total_spectrum of all terms: {time.perf_counter() - t0:.2f} s")
    gc.collect()
    print(f"RSS now +{(rss() - start) / MB:.0f} MB over start")
    est = estimate_bytes(tt)
    print(f"estimate_bytes(tt) {est / MB:.0f} MB; "
          f"would the cache keep it: {est <= (cache.max_bytes or est)}")
    import ctypes
    tt = cols = None
    gc.collect()
    print(f"after dropping the table: RSS +{(rss() - start) / MB:.0f} MB")
    ctypes.CDLL('libc.so.6').malloc_trim(0)
    print(f"after malloc_trim: RSS +{(rss() - start) / MB:.0f} MB")


main()
