""" The job record, claiming, leases and the reaper """
import datetime
import unittest
import mongoengine
from bson import ObjectId
from bgexplorer import jobs
from bgexplorer.jobs import models
from bgexplorer.jobs.models import Job, IllegalTransition
from tests.dbutil import connect_test_db

LEASE = 60


@jobs.job_kind('t_noop')
def _noop(params):
    return {}


@jobs.job_kind('t_edit', serial=True)
def _edit(params):
    return {}


@jobs.job_kind('t_excl', exclusive=True, idempotent=True, max_attempts=2)
def _excl(params):
    return {}


@jobs.job_kind('t_idem', idempotent=True, max_attempts=2)
def _idem(params):
    return {}


def later(seconds):
    return models.utcnow() + datetime.timedelta(seconds=seconds)


class JobTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        mongoengine.disconnect()

    def setUp(self):
        Job.drop_collection()
        Job.ensure_indexes()

    def status(self, job_id):
        return models.get(job_id).status


class TestSubmit(JobTestCase):
    def test_defaults(self):
        job_id = jobs.submit('t_edit', {'a': 1}, scope='main', user='ann',
                             origin='/x')
        job = models.get(job_id)
        self.assertEqual(job.status, 'queued')
        self.assertEqual((job.kind, job.params, job.scope, job.user,
                          job.origin), ('t_edit', {'a': 1}, 'main', 'ann',
                                        '/x'))
        self.assertTrue(job.serial)
        self.assertFalse(job.exclusive)
        self.assertEqual(job.attempts, 0)
        self.assertEqual(job.max_attempts, 3)
        self.assertIsNotNone(job.created)
        self.assertIsNone(job.started)
        self.assertEqual(job.progress.done, 0)
        self.assertIsNone(job.progress.total)

    def test_exclusive_flag_and_attempts_copied(self):
        job = models.get(jobs.submit('t_excl'))
        self.assertTrue(job.exclusive)
        self.assertEqual(job.max_attempts, 2)

    def test_params_must_be_json(self):
        for bad in ({'id': ObjectId()}, {'s': {1, 2}},
                    {'when': datetime.datetime.now()}, {1: 'int key'},
                    {'x': object()}):
            with self.subTest(params=bad):
                with self.assertRaises(ValueError):
                    jobs.submit('t_noop', bad)
        self.assertEqual(Job.objects.count(), 0)

    def test_json_params_accepted(self):
        params = {'a': [1, 2.5, None, True, 'x'], 'b': {'c': {'d': []}}}
        self.assertEqual(models.get(jobs.submit('t_noop', params)).params,
                         params)

    def test_unknown_kind(self):
        with self.assertRaises(KeyError):
            jobs.submit('no_such_kind')

    def test_get_unknown_or_bad_id(self):
        self.assertIsNone(models.get(str(ObjectId())))
        self.assertIsNone(models.get('not an id'))


class TestTransitions(JobTestCase):
    def test_legal(self):
        job_id = jobs.submit('t_noop')
        self.assertTrue(models.transition(job_id, 'running'))
        self.assertTrue(models.transition(job_id, 'succeeded'))
        self.assertEqual(self.status(job_id), 'succeeded')

    def test_requeue_and_cancel(self):
        job_id = jobs.submit('t_noop')
        self.assertTrue(models.transition(job_id, 'cancelled'))
        job_id = jobs.submit('t_noop')
        models.transition(job_id, 'running')
        self.assertTrue(models.transition(job_id, 'queued'))

    def test_illegal(self):
        job_id = jobs.submit('t_noop')
        with self.assertRaises(IllegalTransition):
            models.transition(job_id, 'succeeded', expect='queued')
        models.transition(job_id, 'running')
        models.transition(job_id, 'succeeded')
        # finished jobs go nowhere, and no state can reach 'queued' but running
        for to in ('running', 'failed', 'queued', 'cancelled'):
            with self.subTest(to=to):
                self.assertFalse(models.transition(job_id, to))
        self.assertEqual(self.status(job_id), 'succeeded')

    def test_expect_mismatch(self):
        job_id = jobs.submit('t_noop')
        self.assertFalse(models.transition(job_id, 'succeeded',
                                           expect='running'))

    def test_owner_must_match(self):
        job_id = jobs.submit('t_noop')
        job = models.claim('me', LEASE)
        self.assertEqual(str(job.id), job_id)
        self.assertFalse(models.transition(job_id, 'succeeded',
                                           owner='someone else'))
        self.assertTrue(models.transition(job_id, 'succeeded', owner='me'))


class TestClaim(JobTestCase):
    def test_oldest_first_sets_lease(self):
        first = jobs.submit('t_noop')
        second = jobs.submit('t_noop')
        before = models.utcnow()
        job = models.claim('me', LEASE)
        self.assertEqual(str(job.id), first)
        self.assertEqual(job.status, 'running')
        self.assertEqual(job.lease.owner, 'me')
        self.assertGreaterEqual(job.lease.expires,
                                before + datetime.timedelta(seconds=LEASE - 1))
        self.assertLessEqual(job.lease.expires, models.utcnow()
                             + datetime.timedelta(seconds=LEASE))
        self.assertEqual(job.attempts, 1)
        self.assertIsNotNone(job.started)
        self.assertEqual(str(models.claim('me', LEASE).id), second)
        self.assertIsNone(models.claim('me', LEASE))

    def test_not_before(self):
        job_id = jobs.submit('t_noop')
        Job.objects(id=job_id).update(set__not_before=later(3600))
        self.assertIsNone(models.claim('me', LEASE))
        self.assertIsNotNone(models.claim('me', LEASE, now=later(7200)))

    def test_claim_is_atomic(self):
        jobs.submit('t_noop')
        claimed = [models.claim(f'w{i}', LEASE) for i in range(3)]
        self.assertEqual(sum(c is not None for c in claimed), 1)

    def test_serial_one_per_scope(self):
        a = jobs.submit('t_edit', scope='main')
        b = jobs.submit('t_edit', scope='main')
        c = jobs.submit('t_edit', scope='other')
        self.assertEqual(str(models.claim('me', LEASE).id), a)
        # b is blocked by a, so the next claim is c
        self.assertEqual(str(models.claim('me', LEASE).id), c)
        self.assertIsNone(models.claim('me', LEASE))
        models.finish(a, 'me', 'succeeded')
        self.assertEqual(str(models.claim('me', LEASE).id), b)

    def test_plain_jobs_are_not_serial(self):
        jobs.submit('t_noop', scope='main')
        jobs.submit('t_noop', scope='main')
        self.assertIsNotNone(models.claim('me', LEASE))
        self.assertIsNotNone(models.claim('me', LEASE))

    def test_exclusive_blocks_newer_edits_and_waits_for_running(self):
        old = jobs.submit('t_edit', scope='main')
        excl = jobs.submit('t_excl', scope='main')
        new = jobs.submit('t_edit', scope='main')
        self.assertEqual(str(models.claim('me', LEASE).id), old)
        # the exclusive job waits for the running edit; the newer edit waits
        # for the exclusive one
        self.assertIsNone(models.claim('me', LEASE))
        models.finish(old, 'me', 'succeeded')
        self.assertEqual(str(models.claim('me', LEASE).id), excl)
        self.assertIsNone(models.claim('me', LEASE))
        models.finish(excl, 'me', 'succeeded')
        self.assertEqual(str(models.claim('me', LEASE).id), new)

    def test_exclusive_other_scope_does_not_block(self):
        excl = jobs.submit('t_excl', scope='a')
        edit = jobs.submit('t_edit', scope='b')
        claimed = {str(models.claim('me', LEASE).id),
                   str(models.claim('me', LEASE).id)}
        self.assertEqual(claimed, {excl, edit})

    def test_one_exclusive_per_scope(self):
        a = jobs.submit('t_excl', scope='main')
        jobs.submit('t_excl', scope='main')
        self.assertEqual(str(models.claim('me', LEASE).id), a)
        self.assertIsNone(models.claim('me', LEASE))

    def test_slot_unique_index_is_the_guard(self):
        """ even if the gating query races, two serial jobs for one scope
        can't both be running """
        a = jobs.submit('t_edit', scope='main')
        b = jobs.submit('t_edit', scope='main')
        coll = Job._get_collection()
        coll.update_one({'_id': ObjectId(a)},
                        {'$set': {'status': 'running', 'slot': 'edit:main'}})
        with self.assertRaises(Exception):
            coll.update_one({'_id': ObjectId(b)},
                            {'$set': {'status': 'running',
                                      'slot': 'edit:main'}})

    def test_indexes(self):
        info = Job._get_collection().index_information()
        by_key = {tuple(k for k, _ in v['key']): v for v in info.values()}
        self.assertTrue(by_key[('slot',)].get('unique'))
        self.assertTrue(by_key[('slot',)].get('sparse'))
        self.assertEqual(by_key[('expires_at',)].get('expireAfterSeconds'), 0)
        self.assertIn(('status', 'created'), by_key)
        self.assertIn(('scope', 'status'), by_key)
        self.assertTrue(by_key[('lease.expires',)].get('sparse'))


class TestLeases(JobTestCase):
    def test_reap_requeues_idempotent_else_abandons(self):
        idem = jobs.submit('t_idem')
        plain = jobs.submit('t_noop')
        models.claim('me', LEASE)
        models.claim('me', LEASE)
        reaped = models.reap(now=later(LEASE + 1))
        self.assertEqual(set(reaped), {idem, plain})
        job = models.get(idem)
        self.assertEqual(job.status, 'queued')
        self.assertIsNone(job.lease)
        self.assertIsNone(job.slot)
        gone = models.get(plain)
        self.assertEqual(gone.status, 'abandoned')
        self.assertTrue(gone.error)
        self.assertIsNotNone(gone.finished)
        self.assertIsNotNone(gone.expires_at)
        # nothing left to reap
        self.assertEqual(models.reap(now=later(LEASE * 3)), [])

    def test_reap_gives_up_after_max_attempts(self):
        job_id = jobs.submit('t_idem')
        for attempt in range(2):
            self.assertEqual(self.status(job_id), 'queued')
            models.claim('me', LEASE)
            models.reap(now=later(LEASE + 1))
        job = models.get(job_id)
        self.assertEqual(job.status, 'abandoned')
        self.assertEqual(job.attempts, 2)

    def test_reap_leaves_live_leases(self):
        job_id = jobs.submit('t_idem')
        models.claim('me', LEASE)
        self.assertEqual(models.reap(), [])
        self.assertEqual(self.status(job_id), 'running')

    def test_reap_frees_slot(self):
        a = jobs.submit('t_edit', scope='main')
        b = jobs.submit('t_edit', scope='main')
        models.claim('me', LEASE)
        models.reap(now=later(LEASE + 1))
        self.assertEqual(self.status(a), 'abandoned')
        self.assertEqual(str(models.claim('me', LEASE).id), b)

    def test_renew_only_own_leases(self):
        a = jobs.submit('t_noop')
        b = jobs.submit('t_noop')
        models.claim('me', LEASE)
        models.claim('you', LEASE)
        old = {str(j.id): j.lease.expires for j in Job.objects}
        self.assertEqual(models.renew('me', [a, b], 2 * LEASE), [a])
        new = {str(j.id): j.lease.expires for j in Job.objects}
        self.assertGreater(new[a], old[a])
        self.assertEqual(new[b], old[b])

    def test_renew_after_reap_reports_loss(self):
        a = jobs.submit('t_idem')
        models.claim('me', LEASE)
        models.reap(now=later(LEASE + 1))
        self.assertEqual(models.renew('me', [a], LEASE), [])


class TestFinish(JobTestCase):
    def test_success(self):
        job_id = jobs.submit('t_edit', scope='main')
        models.claim('me', LEASE)
        self.assertTrue(models.finish(job_id, 'me', 'succeeded',
                                      result={'redirect': '/x'},
                                      keep_days=2))
        job = models.get(job_id)
        self.assertEqual(job.result, {'redirect': '/x'})
        self.assertIsNone(job.lease)
        self.assertIsNone(job.slot)
        self.assertGreater(job.expires_at, job.finished
                           + datetime.timedelta(days=1, hours=23))

    def test_failure_keeps_error_and_traceback(self):
        job_id = jobs.submit('t_noop')
        models.claim('me', LEASE)
        models.finish(job_id, 'me', 'failed', error='bad', traceback='tb',
                      error_type='ValueError')
        job = models.get(job_id)
        self.assertEqual((job.error, job.traceback, job.error_type),
                         ('bad', 'tb', 'ValueError'))

    def test_requires_owner(self):
        job_id = jobs.submit('t_noop')
        models.claim('me', LEASE)
        self.assertFalse(models.finish(job_id, 'you', 'succeeded'))
        self.assertEqual(self.status(job_id), 'running')

    def test_cannot_finish_twice(self):
        job_id = jobs.submit('t_noop')
        models.claim('me', LEASE)
        self.assertTrue(models.finish(job_id, 'me', 'succeeded'))
        self.assertFalse(models.finish(job_id, 'me', 'failed'))
        self.assertEqual(self.status(job_id), 'succeeded')

    def test_finish_status_must_be_final(self):
        job_id = jobs.submit('t_noop')
        models.claim('me', LEASE)
        with self.assertRaises(ValueError):
            models.finish(job_id, 'me', 'queued')


class TestRequeueAndCancel(JobTestCase):
    def test_requeue_does_not_count_attempt(self):
        job_id = jobs.submit('t_edit', scope='main')
        models.claim('me', LEASE)
        self.assertTrue(models.requeue(job_id, 'me', delay=30,
                                       message='waiting for the merge'))
        job = models.get(job_id)
        self.assertEqual(job.status, 'queued')
        self.assertEqual(job.attempts, 0)
        self.assertIsNone(job.lease)
        self.assertIsNone(job.slot)
        self.assertEqual(job.progress.message, 'waiting for the merge')
        self.assertGreater(job.not_before, models.utcnow())
        self.assertIsNone(models.claim('me', LEASE))
        self.assertIsNotNone(models.claim('me', LEASE, now=later(31)))

    def test_requeue_requires_owner(self):
        job_id = jobs.submit('t_noop')
        models.claim('me', LEASE)
        self.assertFalse(models.requeue(job_id, 'you'))

    def test_cancel_queued(self):
        job_id = jobs.submit('t_noop')
        self.assertEqual(models.request_cancel(job_id), 'cancelled')
        job = models.get(job_id)
        self.assertEqual(job.status, 'cancelled')
        self.assertIsNotNone(job.finished)

    def test_cancel_running_is_a_request(self):
        job_id = jobs.submit('t_noop')
        models.claim('me', LEASE)
        self.assertEqual(models.request_cancel(job_id), 'requested')
        job = models.get(job_id)
        self.assertEqual(job.status, 'running')
        self.assertTrue(job.cancel_requested)

    def test_cancel_finished(self):
        job_id = jobs.submit('t_noop')
        models.claim('me', LEASE)
        models.finish(job_id, 'me', 'succeeded')
        self.assertEqual(models.request_cancel(job_id), 'finished')
        self.assertEqual(self.status(job_id), 'succeeded')

    def test_cancel_unknown(self):
        self.assertIsNone(models.request_cancel(str(ObjectId())))


class TestQueries(JobTestCase):
    def test_active_for_scope(self):
        a = jobs.submit('t_edit', scope='main')
        b = jobs.submit('t_noop', scope='main')
        jobs.submit('t_noop', scope='other')
        done = jobs.submit('t_noop', scope='main')
        Job.objects(id=done).update(set__status='succeeded')
        models.claim('me', LEASE)
        found = models.active_for_scope('main')
        self.assertEqual({d['id'] for d in found}, {a, b})
        self.assertEqual({d['status'] for d in found}, {'running', 'queued'})
        self.assertEqual(models.active_for_scope('nothing'), [])
        self.assertEqual({d['id'] for d in
                          models.active_for_scope('main', exclude=a)}, {b})

    def test_percent(self):
        job_id = jobs.submit('t_noop')
        job = models.get(job_id)
        self.assertIsNone(job.percent)
        Job.objects(id=job_id).update(set__progress__done=5,
                                      set__progress__total=10)
        self.assertEqual(models.get(job_id).percent, 50)
        Job.objects(id=job_id).update(set__progress__total=0)
        self.assertIsNone(models.get(job_id).percent)


if __name__ == '__main__':
    unittest.main()
