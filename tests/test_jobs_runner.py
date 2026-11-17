""" Running jobs: the context a job sees, executors and the runner """
import contextlib
import os
import threading
import time
import unittest
from bgexplorer import jobs
from bgexplorer.jobs import kinds, models
from bgexplorer.jobs.context import (JobContext, JobCancelled, JobLost, Retry,
                                     running)
from bgexplorer.jobs.executors import make_executor
from tests import jobs_support
from tests.jobs_support import JobTestCase, later

LEASE = 60
PROCESS_TESTS = os.environ.get('BGEXPLORER_TEST_PROCESS', '1') == '1'


@jobs.job_kind('r_noop')
def _noop(params):
    return {}


@jobs.job_kind('r_edit', serial=True)
def _edit(params):
    return {}


@jobs.job_kind('r_excl', exclusive=True)
def _excl(params):
    return {}


@jobs.job_kind('r_idem', idempotent=True)
def _idem(params):
    return {}


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TestContext(JobTestCase):
    def claimed(self, kind='r_noop', scope=None, owner='me', **kw):
        """ a context for a job that is running """
        job_id = jobs.submit(kind, scope=scope)
        job = models.claim(owner, LEASE)
        self.assertEqual(str(job.id), job_id)
        kw.setdefault('progress_interval', 0)
        kw.setdefault('cancel_interval', 0)
        return job_id, JobContext(job_id, owner, 'ann', **kw)

    def progress_of(self, job_id):
        return models.get(job_id).progress

    def test_outside_a_job_everything_is_a_noop(self):
        self.assertIsNone(jobs.progress(1, 10, stage='x', message='y'))
        self.assertIsNone(jobs.check_cancelled())
        self.assertIsNone(jobs.current_user())
        self.assertIsNone(jobs.current_job_id())
        self.assertIsNone(jobs.require_idle('main'))

    def test_current_user_and_id_only_inside(self):
        job_id, ctx = self.claimed()
        with running(ctx):
            self.assertEqual(jobs.current_user(), 'ann')
            self.assertEqual(jobs.current_job_id(), job_id)
        self.assertIsNone(jobs.current_user())
        self.assertIsNone(jobs.current_job_id())

    def test_context_is_restored_after_an_exception(self):
        _, ctx = self.claimed()
        with self.assertRaises(RuntimeError):
            with running(ctx):
                raise RuntimeError()
        self.assertIsNone(jobs.current_job_id())

    def test_progress_is_written(self):
        job_id, ctx = self.claimed()
        with running(ctx):
            jobs.progress(1, 10, stage='loading', message='first')
        progress = self.progress_of(job_id)
        self.assertEqual((progress.done, progress.total, progress.stage,
                          progress.message), (1, 10, 'loading', 'first'))

    def test_progress_keeps_fields_not_given(self):
        job_id, ctx = self.claimed()
        with running(ctx):
            jobs.progress(1, 10, stage='loading', message='first')
            jobs.progress(2)
        progress = self.progress_of(job_id)
        self.assertEqual((progress.done, progress.total, progress.stage,
                          progress.message), (2, 10, 'loading', 'first'))

    def test_progress_is_throttled(self):
        clock = FakeClock()
        job_id, ctx = self.claimed(progress_interval=10)
        ctx.clock = clock
        with running(ctx):
            jobs.progress(1, 10)                      # the first is written
            jobs.progress(2, 10)
            self.assertEqual(self.progress_of(job_id).done, 1)
            jobs.progress(3, 10, stage='next')        # a new stage is
            self.assertEqual(self.progress_of(job_id).done, 3)
            jobs.progress(4, 10)
            self.assertEqual(self.progress_of(job_id).done, 3)
            clock.now = 11                            # so is a late call
            jobs.progress(5, 10)
            self.assertEqual(self.progress_of(job_id).done, 5)
            jobs.progress(6, 10)
            self.assertEqual(self.progress_of(job_id).done, 5)
            jobs.progress(10, 10)                     # and the last
            self.assertEqual(self.progress_of(job_id).done, 10)

    def test_progress_never_goes_back_and_is_clamped(self):
        job_id, ctx = self.claimed()
        with running(ctx):
            jobs.progress(5, 10)
            jobs.progress(3, 10)
            self.assertEqual(self.progress_of(job_id).done, 5)
            jobs.progress(50, 10)
            self.assertEqual(self.progress_of(job_id).done, 10)

    def test_check_cancelled(self):
        job_id, ctx = self.claimed()
        with running(ctx):
            jobs.check_cancelled()
            models.request_cancel(job_id)
            with self.assertRaises(JobCancelled):
                jobs.check_cancelled()

    def test_check_cancelled_is_throttled(self):
        clock = FakeClock()
        job_id, ctx = self.claimed(cancel_interval=10)
        ctx.clock = clock
        with running(ctx):
            jobs.check_cancelled()
            models.request_cancel(job_id)
            jobs.check_cancelled()          # too soon to look again
            clock.now = 11
            with self.assertRaises(JobCancelled):
                jobs.check_cancelled()

    def test_lease_lost_to_the_reaper(self):
        job_id, ctx = self.claimed('r_idem')
        models.reap(now=later(LEASE + 1))
        with running(ctx):
            with self.assertRaises(JobLost):
                jobs.progress(1, 10)
            with self.assertRaises(JobLost):
                jobs.check_cancelled()
        self.assertEqual(self.progress_of(job_id).done, 0)

    def test_lease_taken_by_another_owner(self):
        job_id, ctx = self.claimed('r_idem')
        models.reap(now=later(LEASE + 1))
        models.claim('other', LEASE)
        with running(ctx):
            with self.assertRaises(JobLost):
                jobs.progress(1, 10)
            with self.assertRaises(JobLost):
                jobs.check_cancelled()
        self.assertEqual(self.progress_of(job_id).done, 0)

    def test_finished_job_is_lost(self):
        job_id, ctx = self.claimed()
        models.finish(job_id, 'me', 'succeeded')
        with running(ctx):
            with self.assertRaises(JobLost):
                jobs.progress(1, 10)

    def test_require_idle(self):
        edit_id, edit_ctx = self.claimed('r_edit', scope='main')
        excl_id = jobs.submit('r_excl', scope='main')
        # the exclusion normally prevents this; force the race it covers
        models.Job.objects(id=excl_id).update(
            set__status='running', set__lease=models.Lease(
                owner='me', expires=later(LEASE)))
        excl_ctx = JobContext(excl_id, 'me', None)
        with running(excl_ctx):
            with self.assertRaises(Retry) as caught:
                jobs.require_idle('main')
            self.assertGreater(caught.exception.delay, 0)
            self.assertIsNone(jobs.require_idle('other scope'))
        with running(edit_ctx):
            jobs.require_idle('main')          # itself doesn't count
        models.finish(edit_id, 'me', 'succeeded')
        with running(excl_ctx):
            jobs.require_idle('main')

    def test_retry_has_a_default_delay_and_message(self):
        retry = Retry()
        self.assertGreater(retry.delay, 0)
        retry = Retry(0, 'waiting')
        self.assertEqual((retry.delay, retry.message), (0, 'waiting'))
        self.assertIn('waiting', str(retry))


def _thread_name():
    return threading.current_thread().name


def _boom():
    raise KeyError('boom')


class TestExecutors(unittest.TestCase):
    def test_eager_runs_inline(self):
        executor = make_executor('eager', 4)
        future = executor.submit(_thread_name)
        self.assertTrue(future.done())
        self.assertEqual(future.result(), threading.current_thread().name)

    def test_eager_captures_exceptions(self):
        future = make_executor('eager', 1).submit(_boom)
        self.assertTrue(future.done())
        with self.assertRaises(KeyError):
            future.result()

    def test_eager_calls_done_callbacks(self):
        seen = []
        make_executor('eager', 1).submit(lambda: 7).add_done_callback(
            lambda f: seen.append(f.result()))
        self.assertEqual(seen, [7])

    def test_thread_runs_elsewhere(self):
        executor = make_executor('thread', 2)
        try:
            name = executor.submit(_thread_name).result(timeout=5)
            self.assertNotEqual(name, threading.current_thread().name)
        finally:
            executor.shutdown()

    def test_thread_initializer(self):
        seen = []
        executor = make_executor('thread', 1, initializer=seen.append,
                                 initargs=('ready',))
        try:
            executor.submit(_thread_name).result(timeout=5)
        finally:
            executor.shutdown()
        self.assertEqual(seen, ['ready'])

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            make_executor('bogus', 1)

    @unittest.skipUnless(PROCESS_TESTS, 'BGEXPLORER_TEST_PROCESS is off')
    def test_process_runs_in_another_process(self):
        executor = make_executor('process', 1)
        try:
            self.assertNotEqual(executor.submit(os.getpid).result(timeout=60),
                                os.getpid())
        finally:
            executor.shutdown()


CALLS = []      # what the kinds below did, in order
GATES = {}      # name -> Event that lets a blocked job go on
STARTED = {}    # name -> Event that is set when the job has begun


@jobs.job_kind('r_add')
def _add(params):
    return {'sum': params['a'] + params['b']}


@jobs.job_kind('r_boom')
def _boom_job(params):
    raise ValueError('bad thing\nsecond line')


class _Reported(Exception):
    job_data = {'status': 409, 'why': ['a', 'b']}


@jobs.job_kind('r_reported')
def _reported_job(params):
    raise _Reported('reported thing')


@jobs.job_kind('r_retry')
def _retry(params):
    CALLS.append('retry')
    raise Retry(0, 'waiting')


@jobs.job_kind('r_retry_on', retry_on=(KeyError,))
def _retry_on(params):
    CALLS.append('retry_on')
    raise KeyError('locked')


@jobs.job_kind('r_cancel')
def _cancel(params):
    raise JobCancelled()


@jobs.job_kind('r_bad_result')
def _bad_result(params):
    return {'x': object()}


@jobs.job_kind('r_nothing')
def _nothing(params):
    return None


@jobs.job_kind('r_user')
def _user(params):
    CALLS.append(('run', jobs.current_user()))
    return {}


@jobs.job_kind('r_reap_self', idempotent=True)
def _reap_self(params):
    models.reap(now=later(LEASE + 1))
    jobs.progress(1, 2)
    return {'reached': 'the end'}


@jobs.job_kind('r_idle_excl', exclusive=True)
def _idle_excl(params):
    jobs.require_idle(params['scope'])
    return {}


def _block(params):
    name = params['name']
    STARTED[name].set()
    while not GATES[name].wait(0.02):
        jobs.check_cancelled()
    jobs.progress(1, 1)
    return {'name': name}


jobs.job_kind('r_block')(_block)
jobs.job_kind('r_block_edit', serial=True)(_block)
jobs.job_kind('r_block_excl', exclusive=True)(_block)
jobs.job_kind('r_block_idem', idempotent=True)(_block)


class RunnerCase(JobTestCase):
    def setUp(self):
        super().setUp()
        self.runners = []
        CALLS.clear()

    def tearDown(self):
        for gate in GATES.values():
            gate.set()
        for runner in self.runners:
            runner.stop()
        GATES.clear()
        STARTED.clear()
        jobs.configure(user_context=None)

    def gate(self, *names):
        for name in names:
            GATES[name] = threading.Event()
            STARTED[name] = threading.Event()

    def runner(self, mode='thread', **kw):
        kw.setdefault('workers', 3)
        kw.setdefault('lease_seconds', 30)
        kw.setdefault('poll_seconds', 0.05)
        kw.setdefault('progress_interval', 0)
        kw.setdefault('cancel_interval', 0.05)
        kw.setdefault('shutdown_seconds', 5)
        runner = jobs.JobRunner(mode, **kw)
        runner.start()
        self.runners.append(runner)
        return runner

    def eventually(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def begun(self, name, timeout=5):
        return STARTED[name].wait(timeout)

    def not_begun(self, name):
        return not STARTED[name].wait(0.3)


class TestExecute(RunnerCase):
    """ What happens to a job after the kind is called, without a runner """
    def run_job(self, kind, params=None, scope=None, user=None):
        job_id = jobs.submit(kind, params, scope=scope, user=user)
        self.assertEqual(str(models.claim('me', LEASE).id), job_id)
        return job_id, jobs.execute(job_id, 'me')

    def test_success_stores_the_result(self):
        job_id, outcome = self.run_job('r_add', {'a': 1, 'b': 2})
        job = models.get(job_id)
        self.assertEqual(outcome, 'succeeded')
        self.assertEqual((job.status, job.result), ('succeeded', {'sum': 3}))
        self.assertIsNone(job.lease)
        self.assertIsNotNone(job.finished)
        self.assertIsNotNone(job.expires_at)

    def test_no_result_is_an_empty_one(self):
        job_id, _ = self.run_job('r_nothing')
        job = models.get(job_id)
        self.assertEqual((job.status, job.result), ('succeeded', {}))

    def test_failure_records_the_error(self):
        job_id, outcome = self.run_job('r_boom')
        job = models.get(job_id)
        self.assertEqual(outcome, 'failed')
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error, 'bad thing')
        self.assertEqual(job.error_type, 'ValueError')
        self.assertIn('Traceback', job.traceback)
        self.assertIn('_boom_job', job.traceback)
        self.assertIsNone(job.lease)

    def test_a_failure_keeps_the_data_its_error_carries(self):
        job_id, outcome = self.run_job('r_reported')
        job = models.get(job_id)
        self.assertEqual((outcome, job.status), ('failed', 'failed'))
        self.assertEqual(job.result, {'status': 409, 'why': ['a', 'b']})
        self.assertEqual(job.error, 'reported thing')
        self.assertFalse(models.get(self.run_job('r_boom')[0]).result)

    def test_a_result_that_is_not_json_fails_the_job(self):
        job_id, _ = self.run_job('r_bad_result')
        job = models.get(job_id)
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error_type, 'ValueError')

    def test_retry_queues_again_without_using_an_attempt(self):
        job_id, outcome = self.run_job('r_retry')
        job = models.get(job_id)
        self.assertEqual((outcome, job.status), ('queued', 'queued'))
        self.assertEqual(job.attempts, 0)
        self.assertEqual(job.progress.message, 'waiting')
        self.assertIsNone(job.lease)
        self.assertIsNone(job.finished)

    def test_kind_retry_on_is_a_retry(self):
        job_id, outcome = self.run_job('r_retry_on')
        job = models.get(job_id)
        self.assertEqual((outcome, job.status), ('queued', 'queued'))
        self.assertEqual(job.attempts, 0)
        self.assertIn('locked', job.progress.message)
        self.assertGreater(job.not_before, models.utcnow())

    def test_cancelled(self):
        job_id, outcome = self.run_job('r_cancel')
        self.assertEqual((outcome, self.status(job_id)), ('cancelled',) * 2)

    def test_unknown_kind_fails(self):
        job_id = jobs.submit('r_noop')
        job = models.claim('me', LEASE)
        spec = kinds._KINDS.pop('r_noop')
        try:
            self.assertEqual(jobs.execute(job_id, 'me'), 'failed')
        finally:
            kinds._KINDS['r_noop'] = spec
        job = models.get(job_id)
        self.assertEqual((job.status, job.error_type), ('failed', 'KeyError'))

    def test_a_lost_job_is_left_alone(self):
        job_id, outcome = self.run_job('r_reap_self')
        job = models.get(job_id)
        self.assertEqual(outcome, 'lost')
        self.assertEqual(job.status, 'queued')    # as the reaper left it
        self.assertEqual(job.result, {})
        self.assertEqual(job.progress.done, 0)

    def test_job_leased_to_someone_else_is_not_run(self):
        job_id = jobs.submit('r_boom')
        models.claim('them', LEASE)
        self.assertEqual(jobs.execute(job_id, 'me'), 'lost')
        self.assertEqual(self.status(job_id), 'running')
        self.assertEqual(jobs.execute('not an id', 'me'), 'lost')

    def test_user_context_wraps_the_kind(self):
        @contextlib.contextmanager
        def as_user(user):
            CALLS.append(('enter', user))
            try:
                yield
            finally:
                CALLS.append(('exit', user))

        jobs.configure(user_context=as_user)
        self.run_job('r_user', user='ann')
        self.assertEqual(CALLS, [('enter', 'ann'), ('run', 'ann'),
                                 ('exit', 'ann')])

    def test_exclusive_waits_for_running_edit_after_lock(self):
        edit_id = jobs.submit('r_edit', scope='main')
        models.claim('me', LEASE)
        excl_id = jobs.submit('r_idle_excl', {'scope': 'main'}, scope='main')
        # the exclusion normally prevents this; force the race it covers
        models.Job.objects(id=excl_id).update(
            set__status='running', set__attempts=1, set__lease=models.Lease(
                owner='me', expires=later(LEASE)))
        self.assertEqual(jobs.execute(excl_id, 'me'), 'queued')
        job = models.get(excl_id)
        self.assertEqual((job.status, job.attempts), ('queued', 0))
        self.assertIn('Waiting', job.progress.message)
        models.finish(edit_id, 'me', 'succeeded')
        self.assertEqual(str(models.claim('me', LEASE, now=later(5)).id),
                         excl_id)
        self.assertEqual(jobs.execute(excl_id, 'me'), 'succeeded')

    def test_configure_rejects_unknown_settings(self):
        with self.assertRaises(TypeError):
            jobs.configure(bogus=1)


class TestRunner(RunnerCase):
    def test_eager_submit_runs_the_job_before_returning(self):
        runner = self.runner('eager')
        job_id = runner.submit('r_add', {'a': 1, 'b': 2}, scope='s',
                               user='ann', origin='/page')
        job = models.get(job_id)
        self.assertEqual((job.status, job.result), ('succeeded', {'sum': 3}))
        self.assertEqual((job.scope, job.user, job.origin),
                         ('s', 'ann', '/page'))
        self.assertEqual(job.attempts, 1)
        self.assertEqual(runner.running_ids(), [])

    def test_eager_failure(self):
        runner = self.runner('eager')
        job = models.get(runner.submit('r_boom'))
        self.assertEqual((job.status, job.error), ('failed', 'bad thing'))

    def test_eager_retry_runs_once_per_drain(self):
        runner = self.runner('eager')
        job_id = runner.submit('r_retry')
        self.assertEqual(CALLS, ['retry'])
        self.assertEqual(self.status(job_id), 'queued')
        self.assertEqual(runner.drain(), 1)
        self.assertEqual(CALLS, ['retry', 'retry'])

    def test_eager_drain_runs_what_is_queued(self):
        runner = self.runner('eager')
        ids = [jobs.submit('r_add', {'a': i, 'b': 1}) for i in range(3)]
        self.assertEqual(self.status(ids[0]), 'queued')
        self.assertEqual(runner.drain(), 3)
        self.assertEqual([self.status(i) for i in ids], ['succeeded'] * 3)
        self.assertEqual(runner.drain(), 0)

    def test_wait_returns_the_finished_job(self):
        runner = self.runner('eager')
        job_id = runner.submit('r_add', {'a': 1, 'b': 1})
        self.assertEqual(runner.wait(job_id, 1).result, {'sum': 2})
        self.assertIsNone(runner.wait('not an id', 1))

    def test_wait_gives_up_and_returns_the_running_job(self):
        self.gate('a')
        runner = self.runner()
        job_id = runner.submit('r_block', {'name': 'a'})
        job = runner.wait(job_id, 0.3)
        self.assertEqual(job.status, 'running')

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            jobs.JobRunner('bogus')

    def test_submit_returns_while_the_job_runs(self):
        self.gate('a')
        runner = self.runner()
        job_id = runner.submit('r_block', {'name': 'a'}, user='ann')
        self.assertTrue(self.begun('a'))
        job = models.get(job_id)
        self.assertEqual(job.status, 'running')
        self.assertEqual(job.lease.owner, runner.owner)
        self.assertIn(job_id, runner.running_ids())
        GATES['a'].set()
        job = runner.wait(job_id, 5)
        self.assertEqual((job.status, job.result), ('succeeded',
                                                    {'name': 'a'}))
        self.assertEqual(job.progress.done, 1)
        self.assertTrue(self.eventually(lambda: runner.running_ids() == []))

    def test_cancelling_a_running_job(self):
        self.gate('a')
        runner = self.runner()
        job_id = runner.submit('r_block', {'name': 'a'})
        self.assertTrue(self.begun('a'))
        self.assertEqual(models.request_cancel(job_id), 'requested')
        self.assertEqual(runner.wait(job_id, 5).status, 'cancelled')

    def test_workers_limit_how_many_jobs_run(self):
        self.gate('a', 'b', 'c')
        runner = self.runner(workers=2)
        ids = [runner.submit('r_block', {'name': n}) for n in 'abc']
        self.assertTrue(self.begun('a') and self.begun('b'))
        self.assertTrue(self.not_begun('c'))
        self.assertEqual(self.status(ids[2]), 'queued')
        GATES['a'].set()
        self.assertTrue(self.begun('c'))

    def test_serial_one_per_scope(self):
        self.gate('e1', 'e2', 'e3')
        runner = self.runner()
        runner.submit('r_block_edit', {'name': 'e1'}, scope='main')
        runner.submit('r_block_edit', {'name': 'e2'}, scope='main')
        runner.submit('r_block_edit', {'name': 'e3'}, scope='other')
        self.assertTrue(self.begun('e1') and self.begun('e3'))
        self.assertTrue(self.not_begun('e2'))
        GATES['e1'].set()
        self.assertTrue(self.begun('e2'))

    def test_exclusive_not_starved(self):
        self.gate('e1', 'x', 'e2')
        runner = self.runner()
        runner.submit('r_block_edit', {'name': 'e1'}, scope='main')
        runner.submit('r_block_excl', {'name': 'x'}, scope='main')
        runner.submit('r_block_edit', {'name': 'e2'}, scope='main')
        self.assertTrue(self.begun('e1'))
        self.assertTrue(self.not_begun('x'))
        self.assertTrue(self.not_begun('e2'))
        GATES['e1'].set()
        self.assertTrue(self.begun('x'))
        self.assertTrue(self.not_begun('e2'))
        GATES['x'].set()
        self.assertTrue(self.begun('e2'))

    def test_lease_renewal_keeps_a_running_job(self):
        self.gate('a')
        runner = self.runner(lease_seconds=0.3)
        job_id = runner.submit('r_block_idem', {'name': 'a'})
        self.assertTrue(self.begun('a'))
        time.sleep(1.0)
        job = models.get(job_id)
        self.assertEqual((job.status, job.attempts), ('running', 1))
        self.assertGreater(job.lease.expires, models.utcnow())
        GATES['a'].set()
        self.assertEqual(runner.wait(job_id, 5).status, 'succeeded')

    def test_job_of_a_runner_that_stops_renewing_is_abandoned(self):
        self.gate('a')
        runner = self.runner(lease_seconds=0.3)
        job_id = runner.submit('r_block', {'name': 'a'})
        self.assertTrue(self.begun('a'))
        runner._heartbeat_stop.set()
        self.assertTrue(self.eventually(
            lambda: self.status(job_id) == 'abandoned'))
        GATES['a'].set()    # the job goes on, finds it is lost and stops
        self.assertTrue(self.eventually(lambda: runner.running_ids() == []))
        job = models.get(job_id)
        self.assertEqual(job.status, 'abandoned')
        self.assertEqual(job.progress.done, 0)
        self.assertIn('stopped responding', job.error)

    def test_stop_requeues_idempotent_jobs_and_leaves_the_rest(self):
        self.gate('idem', 'plain')
        runner = self.runner()
        idem = runner.submit('r_block_idem', {'name': 'idem'})
        plain = runner.submit('r_block', {'name': 'plain'})
        self.assertTrue(self.begun('idem') and self.begun('plain'))
        runner.stop(wait=False)
        job = models.get(idem)
        self.assertEqual((job.status, job.attempts), ('queued', 0))
        self.assertIsNone(job.lease)
        self.assertEqual(self.status(plain), 'running')
        runner.stop(wait=False)            # again does no harm
        self.assertEqual(self.status(plain), 'running')

    def test_stop_waits_for_running_jobs(self):
        self.gate('a')
        runner = self.runner()
        job_id = runner.submit('r_block', {'name': 'a'})
        self.assertTrue(self.begun('a'))
        threading.Timer(0.3, GATES['a'].set).start()
        runner.stop()
        self.assertEqual(self.status(job_id), 'succeeded')

    def test_stopped_runner_claims_nothing(self):
        runner = self.runner()
        runner.stop()
        job_id = jobs.submit('r_add', {'a': 1, 'b': 1})
        runner.wake()
        time.sleep(0.3)
        self.assertEqual(self.status(job_id), 'queued')

    @unittest.skipUnless(PROCESS_TESTS, 'BGEXPLORER_TEST_PROCESS is off')
    def test_process_mode(self):
        runner = self.runner('process', workers=1, shutdown_seconds=30,
                             initializer=jobs_support.init)
        good = runner.submit('ps_add', {'a': 2, 'b': 3})
        bad = runner.submit('ps_boom')
        job = runner.wait(good, 90)
        self.assertEqual((job.status, job.result['sum']), ('succeeded', 5))
        self.assertNotEqual(job.result['pid'], os.getpid())
        self.assertEqual((job.progress.done, job.progress.total), (2, 2))
        job = runner.wait(bad, 30)
        self.assertEqual((job.status, job.error, job.error_type),
                         ('failed', 'process failure', 'ValueError'))

    @unittest.skipUnless(PROCESS_TESTS, 'BGEXPLORER_TEST_PROCESS is off')
    def test_process_death_abandons_the_job_and_the_pool_recovers(self):
        runner = self.runner('process', workers=1, shutdown_seconds=30,
                             initializer=jobs_support.init)
        died = runner.submit('ps_die')
        job = runner.wait(died, 90)
        self.assertEqual(job.status, 'abandoned')
        self.assertTrue(job.error)
        after = runner.submit('ps_add', {'a': 1, 'b': 1})
        self.assertEqual(runner.wait(after, 90).status, 'succeeded')


if __name__ == '__main__':
    unittest.main()
