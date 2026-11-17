""" The calculation caches are shared by the threads of a job runner and of the
web server, so they must stand up to concurrent use. They passed the audit
without changes: these tests keep them that way """
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from bgexplorer.models import sourceterm, termcalc, users
from bgexplorer.models.sourceterm import ResultsCache
from bgexplorer.models.common import units

THREADS = 8
ROUNDS = 200


def hammer(work, threads=THREADS):
    """ Call `work(thread number)` in several threads that start together,
    returning their results and raising the first exception """
    barrier = threading.Barrier(threads)

    def run(number):
        barrier.wait()
        return work(number)

    with ThreadPoolExecutor(threads) as pool:
        return list(pool.map(run, range(threads)))


class TestResultsCache(unittest.TestCase):
    def setUp(self):
        self.created = len(ResultsCache.instances)

    def tearDown(self):
        del ResultsCache.instances[self.created:]

    def cache(self, size=50, max_bytes=None):
        return ResultsCache(size, max_bytes, sizeof=lambda value: 10,
                            name='test')

    def consistent(self, cache):
        self.assertEqual(cache.nbytes, sum(cache._sizes.values()))
        self.assertEqual(set(cache._entries), set(cache._sizes))
        self.assertEqual({key for keys in cache._tokens.values()
                          for key in keys}, set(cache._entries))
        self.assertLessEqual(len(cache), cache.size)

    def test_concurrent_get_put_and_purge(self):
        cache = self.cache()

        def work(number):
            for i in range(ROUNDS):
                token = i % 5
                cache.get((token, i % 17), lambda: (token, i))
                cache.put_many({(token, 100 + i % 7): i})
                cache.get_many([(token, 1), (token, 2), (token, 100)])
                if i % 11 == number:
                    cache.purge_token(token)
        hammer(work)
        self.consistent(cache)

    def test_concurrent_use_with_a_byte_limit(self):
        cache = self.cache(size=1000, max_bytes=100)
        hammer(lambda number: [cache.get((i % 3, i), lambda: i)
                               for i in range(ROUNDS)])
        self.consistent(cache)
        self.assertLessEqual(cache.nbytes, 100)

    def test_concurrent_clear(self):
        cache = self.cache()

        def work(number):
            for i in range(ROUNDS):
                cache.get((0, i % 20), lambda: i)
                if i % 25 == number:
                    cache.clear()
        hammer(work)
        self.consistent(cache)

    def test_a_missing_key_is_calculated_once(self):
        cache = self.cache()
        calls = []
        started = threading.Event()

        def calculate():
            calls.append(threading.get_ident())
            started.wait(0.2)
            return 'result'
        results = hammer(lambda number: cache.get((0, 'key'), calculate))
        self.assertEqual(results, ['result'] * THREADS)
        self.assertEqual(len(calls), 1)

    def test_a_failed_calculation_lets_another_thread_try(self):
        cache = self.cache()
        attempts = []

        def calculate():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError('first attempt fails')
            return 'ok'

        def work(number):
            try:
                return cache.get((0, 'key'), calculate)
            except RuntimeError:
                return 'failed'
        results = hammer(work)
        self.assertEqual(results.count('failed'), 1)
        self.assertEqual(results.count('ok'), THREADS - 1)

    def test_current_token_changes_purge_every_cache(self):
        caches = [self.cache(), self.cache()]
        for cache in caches:
            cache.put_many({(('old', n), 1): n for n in range(THREADS)})
        sourceterm._current_tokens.pop('threadsafety', None)

        def work(number):
            sourceterm._note_token('threadsafety', ('old', number))
            return sourceterm._current_tokens['threadsafety']
        try:
            hammer(work)
            sourceterm._note_token('threadsafety', 'newest')
            for cache in caches:
                self.assertEqual(len(cache), 0)
                self.consistent(cache)
        finally:
            sourceterm._current_tokens.pop('threadsafety', None)


class TestSmallSharedState(unittest.TestCase):
    def test_conversion_factors(self):
        # a dict whose setdefault is atomic under the interpreter lock
        results = hammer(lambda number: [
            termcalc.conversion_factor(units.Unit('mBq'), units.Unit('Bq'))
            for _ in range(ROUNDS)])
        self.assertTrue(all(value == 1e-3 for row in results for value in row))

    def test_dummy_password_hash_is_one_value(self):
        # two threads may both hash it, but all use the first appended
        users._dummy.clear()
        try:
            found = hammer(lambda number: users._dummy_hash())
            self.assertEqual(len(set(found)), 1)
        finally:
            users._dummy.clear()


if __name__ == '__main__':
    unittest.main()
