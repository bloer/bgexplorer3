""" Running jobs: the context a job sees, executors and the runner """
import unittest
from bgexplorer import jobs
from bgexplorer.jobs import models, context
from bgexplorer.jobs.context import (JobContext, JobCancelled, JobLost, Retry,
                                     running)
from tests.jobs_support import JobTestCase, later

LEASE = 60


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


if __name__ == '__main__':
    unittest.main()
