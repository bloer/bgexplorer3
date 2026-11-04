""" Version locks belong to their holder, expire unless refreshed, and are
taken by every operation that needs a version to stay as it is """
import datetime
import threading
import time
import unittest
from unittest import mock
from bson import ObjectId
from mongoengine import disconnect
from bgexplorer.versioning import control as vc
from bgexplorer.versioning import record as record_module
from bgexplorer.models.component import Component
from bgexplorer.versioning.history import VersionEvent, EventAction
from bgexplorer.versioning.maintenance import find_locks, clear_locks
from bgexplorer.versioning.locks import acquire_lock, release_lock, refresh_lock, hold_locks, holding
from bgexplorer.versioning.record import VersionRecord, get_record
from bgexplorer.versioning.verdoc import VersionLockedError, LockLostError
from bgexplorer.versioning.verdoc import versioned_classes
from tests.dbutil import connect_test_db

SECOND = datetime.timedelta(seconds=1)


def expire(tag, ago=SECOND):
    """ Make the lock on `tag` expire `ago` before now """
    VersionRecord.objects(version_tag=tag).update(
        set__lock__expires=record_module.now() - ago)


class LockTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in [VersionRecord, VersionEvent] + versioned_classes():
            cls.drop_collection()
        vc.create_version('b')
        Component(name='c1', mass='1 kg', version_tag='b').save()
        vc.create_version('b2', 'b')

    def get(self, tag='b'):
        return Component.objects.get(version_tags=tag, name='c1')

    def write(self, tag='b', mass='2 kg'):
        c1 = self.get(tag)
        c1.mass = mass
        c1.save()

    def lock_of(self, tag):
        return get_record(tag).active_lock

    def heartbeats(self):
        return [t for t in threading.enumerate()
                if t.name == 'version-lock-heartbeat']

    def recording(self, owner, name, tags):
        """ Patch `owner.name` to record which of `tags` are locked when it
        is called, and call through. Returns the record and the patch
        """
        seen = []
        real = getattr(owner, name)

        def call(*args, **kwargs):
            seen.append({t: self.lock_of(t) is not None for t in tags})
            return real(*args, **kwargs)
        return seen, mock.patch.object(owner, name, side_effect=call)


class TestTokenBinding(LockTestCase):
    def test_holder_refused_after_lock_cleared(self):
        with hold_locks(['b'], 'test'):
            self.write()
            release_lock('b')
            with self.assertRaises(LockLostError):
                self.write(mass='3 kg')
        self.assertEqual(self.get().mass.m, 2)

    def test_holder_refused_after_lock_retaken(self):
        with hold_locks(['b'], 'test'):
            release_lock('b')
            other = acquire_lock('b', 'someone else')
            with self.assertRaises(LockLostError):
                self.write()
        # leaving the context doesn't release the other's lock
        self.assertEqual(self.lock_of('b').token, other)
        self.assertEqual(self.get().mass.m, 1)

    def test_other_thread_is_not_the_holder(self):
        errors = []

        def attempt():
            try:
                self.write()
            except VersionLockedError as e:
                errors.append(e)

        with hold_locks(['b'], 'test'):
            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join()
            self.write()
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.get().mass.m, 2)

    def test_reentrant(self):
        with hold_locks(['b'], 'outer'):
            token = self.lock_of('b').token
            with hold_locks(['b', 'b2'], 'inner'):
                self.assertEqual(self.lock_of('b').token, token)
                self.assertIsNotNone(self.lock_of('b2'))
            # the inner context only releases what it took
            self.assertEqual(self.lock_of('b').token, token)
            self.assertIsNone(self.lock_of('b2'))
            self.write()
        self.assertIsNone(self.lock_of('b'))


class TestExpiry(LockTestCase):
    def test_expired_lock_is_free(self):
        old = acquire_lock('b', 'crashed')
        self.assertIsNotNone(self.lock_of('b'))
        expire('b')
        self.assertIsNone(self.lock_of('b'))
        self.write()
        token = acquire_lock('b', 'new')
        self.assertNotEqual(token, old)
        self.assertEqual(self.lock_of('b').reason, 'new')
        # the old holder can't release or refresh the new lock
        self.assertFalse(release_lock('b', old))
        self.assertFalse(refresh_lock('b', old))
        self.assertTrue(release_lock('b', token))

    def test_live_lock_not_taken(self):
        acquire_lock('b', 'first', ttl=datetime.timedelta(minutes=1))
        with self.assertRaises(VersionLockedError):
            acquire_lock('b', 'second')

    def test_no_ttl_never_expires(self):
        acquire_lock('b', 'forever', ttl=None)
        self.assertIsNone(get_record('b').lock.expires)
        with self.assertRaises(VersionLockedError):
            acquire_lock('b', 'second')

    def test_lock_without_expiry_field(self):
        # locks saved before they could expire
        acquire_lock('b', 'old style')
        VersionRecord._get_collection().update_one(
            {'version_tag': 'b'}, {'$unset': {'lock.expires': ''}})
        self.assertIsNotNone(self.lock_of('b'))
        with self.assertRaises(VersionLockedError):
            self.write()
        with self.assertRaises(VersionLockedError):
            acquire_lock('b', 'second')

    def test_refresh(self):
        ttl = datetime.timedelta(minutes=1)
        token = acquire_lock('b', 'test', ttl=ttl)
        expires = get_record('b').lock.expires
        time.sleep(0.01)
        self.assertTrue(refresh_lock('b', token, ttl=2 * ttl))
        self.assertGreater(get_record('b').lock.expires,
                           expires + ttl / 2)
        self.assertFalse(refresh_lock('b', ObjectId()))
        self.assertFalse(refresh_lock('nosuchversion', token))
        # an expired lock that nobody took over can still be refreshed
        expire('b')
        self.assertIsNone(self.lock_of('b'))
        self.assertTrue(refresh_lock('b', token))
        self.assertIsNotNone(self.lock_of('b'))

    def test_holder_may_write_after_expiry_if_not_taken(self):
        with hold_locks(['b'], 'test', ttl=None):
            expire('b')
            self.write()


class TestHeartbeat(LockTestCase):
    def test_keeps_lock_alive(self):
        ttl = datetime.timedelta(seconds=0.6)
        with hold_locks(['b'], 'long operation', ttl=ttl):
            self.assertEqual(len(self.heartbeats()), 1)
            time.sleep(1.8)
            self.assertIsNotNone(self.lock_of('b'))
            with self.assertRaises(VersionLockedError):
                acquire_lock('b', 'someone else')
            self.write()
        self.assertEqual(self.heartbeats(), [])
        self.assertIsNone(self.lock_of('b'))

    def test_without_heartbeat_it_expires(self):
        token = acquire_lock('b', 'test', ttl=datetime.timedelta(seconds=0.3))
        time.sleep(0.5)
        self.assertIsNone(self.lock_of('b'))
        release_lock('b', token)

    def test_stops_on_failure(self):
        with self.assertRaises(RuntimeError):
            with hold_locks(['b', 'b2'], 'test'):
                raise RuntimeError()
        self.assertEqual(self.heartbeats(), [])

    def test_holding_given_tokens(self):
        token = acquire_lock('b', 'test')
        with holding({'b': token}):
            self.assertEqual(len(self.heartbeats()), 1)
            self.write()
        self.assertEqual(self.heartbeats(), [])
        self.assertIsNone(self.lock_of('b'))


class TestMaintenance(LockTestCase):
    def test_find_and_clear(self):
        self.assertEqual(find_locks(), [])
        acquire_lock('b', 'a merge', user='someone')
        acquire_lock('b2', 'crashed')
        expire('b2')
        found = {info.version: info for info in find_locks()}
        self.assertEqual(set(found), {'b', 'b2'})
        self.assertFalse(found['b'].expired)
        self.assertEqual(found['b'].reason, 'a merge')
        self.assertEqual(found['b'].user, 'someone')
        self.assertTrue(found['b2'].expired)

        self.assertEqual(clear_locks(expired_only=True), ['b2'])
        self.assertIsNotNone(get_record('b').lock)
        self.assertIsNone(get_record('b2').lock)

        self.assertEqual(clear_locks(), ['b'])
        self.assertEqual(find_locks(), [])
        self.assertEqual(clear_locks(), [])
        actions = [(e.action, e.version) for e in VersionEvent.objects
                   if e.action == EventAction.clear_lock]
        self.assertCountEqual(actions, [(EventAction.clear_lock, 'b'),
                                        (EventAction.clear_lock, 'b2')])

    def test_clear_one(self):
        acquire_lock('b', 'x')
        acquire_lock('b2', 'y')
        self.assertEqual(clear_locks(['b2']), ['b2'])
        self.assertIsNotNone(get_record('b').lock)
        self.assertIsNone(get_record('b2').lock)


class TestOperationsLock(LockTestCase):
    def test_delete_holds_lock(self):
        seen, patch = self.recording(vc, 'purge_version', ['b2'])
        with patch:
            vc.delete_version('b2')
        self.assertEqual(seen, [{'b2': True}])
        self.assertFalse(vc.version_exists('b2'))

    def test_delete_refused_while_locked(self):
        acquire_lock('b2', 'another operation')
        with self.assertRaises(VersionLockedError):
            vc.delete_version('b2')
        self.assertTrue(vc.version_exists('b2'))

    def test_delete_missing(self):
        with self.assertRaises(KeyError):
            vc.delete_version('nosuch')

    def test_create_locks_source_and_new_version(self):
        seen, patch = self.recording(Component, 'create_tag', ['b', 'b3'])
        with patch:
            vc.create_version('b3', 'b')
        self.assertEqual(seen, [{'b': True, 'b3': True}])
        self.assertIsNone(self.lock_of('b'))
        self.assertIsNone(self.lock_of('b3'))
        self.assertEqual(self.get('b3').name, 'c1')

    def test_create_refused_while_source_locked(self):
        acquire_lock('b', 'a merge')
        with self.assertRaises(VersionLockedError):
            vc.create_version('b3', 'b')
        self.assertFalse(vc.version_exists('b3'))

    def test_create_from_locked_tag_refused(self):
        vc.create_tag('t', 'b')
        acquire_lock('t', 'a merge')
        with self.assertRaises(VersionLockedError):
            vc.create_version('t2', 't')
        self.assertFalse(vc.version_exists('t2'))

    def test_failed_create_is_removed(self):
        with mock.patch.object(Component, 'create_tag',
                               side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                vc.create_version('b3', 'b')
        self.assertFalse(vc.version_exists('b3'))
        self.assertEqual(Component.objects(version_tags='b3').count(), 0)
        self.assertIsNone(self.lock_of('b'))
        self.assertEqual(self.get('b').mass.m, 1)
        vc.create_version('b3', 'b')

    def test_create_over_existing_version_fails(self):
        before = get_record('b2').cache_token
        with self.assertRaises(KeyError):
            vc.create_version('b2', 'b')
        self.assertEqual(get_record('b2').cache_token, before)
        # a creation that raced past the existence check doesn't overwrite
        with mock.patch.object(vc, 'verify_version'):
            with self.assertRaises(KeyError):
                vc.create_version('b2', 'b')
        self.assertEqual(get_record('b2').cache_token, before)
        self.assertEqual(self.get('b2').name, 'c1')
        self.assertIsNone(self.lock_of('b'))

    def test_no_duplicate_tags_when_copy_repeats(self):
        Component.create_tag('b3', 'b')
        Component.create_tag('b3', 'b')
        doc = Component._get_collection().find_one(
            {'name': 'c1', 'version_tags': 'b3'})
        self.assertEqual(doc['version_tags'].count('b3'), 1)


class TestMergeAndImportLock(LockTestCase):
    def setUp(self):
        super().setUp()
        self.write('b2', '5 kg')

    def test_merge_locks_both_versions(self):
        seen, patch = self.recording(vc, '_adopt_copies', ['b', 'b2'])
        with patch:
            plan = vc.merge_version('b2', 'b')
        self.assertTrue(plan.changes)
        self.assertTrue(seen)
        self.assertTrue(all(r == {'b': True, 'b2': True} for r in seen), seen)
        self.assertIsNone(self.lock_of('b'))
        self.assertIsNone(self.lock_of('b2'))
        self.assertEqual(self.get('b').mass.m, 5)

    def test_merge_locks_tag_source(self):
        vc.create_tag('t', 'b2')
        seen, patch = self.recording(vc, '_adopt_copies', ['b', 't'])
        with patch:
            vc.merge_version('t', 'b')
        self.assertTrue(seen)
        self.assertTrue(all(r == {'b': True, 't': True} for r in seen), seen)
        self.assertIsNone(self.lock_of('t'))

    def test_failed_restore_keeps_target_locked(self):
        with mock.patch('bgexplorer.models.signals._update_sourceterms',
                               side_effect=RuntimeError('merge broke')), \
             mock.patch.object(vc, '_restore',
                               side_effect=RuntimeError('restore broke')):
            with self.assertRaises(vc.MergeError) as cm:
                vc.merge_version('b2', 'b')
        self.assertIn('restoring', str(cm.exception))
        lock = get_record('b').lock
        self.assertIsNotNone(lock)
        self.assertIsNone(lock.expires)
        self.assertIn('merge', lock.reason)
        with self.assertRaises(VersionLockedError):
            self.write()
        # and the source is free again
        self.assertIsNone(self.lock_of('b2'))
        self.assertEqual(self.heartbeats(), [])
        # an administrator can clear it
        self.assertEqual(clear_locks(), ['b'])

    def test_successful_rollback_releases(self):
        with mock.patch('bgexplorer.models.signals._update_sourceterms',
                               side_effect=RuntimeError('merge broke')):
            with self.assertRaises(vc.MergeError):
                vc.merge_version('b2', 'b')
        self.assertIsNone(self.lock_of('b'))
        self.assertEqual(self.get('b').mass.m, 1)

    def test_import_locks_both_versions(self):
        c1 = self.get('b2')
        seen, patch = self.recording(vc, '_adopt_copies', ['b', 'b2'])
        with patch:
            vc.import_document(Component, c1.original_id, 'b2', 'b')
        self.assertTrue(seen)
        self.assertTrue(all(r == {'b': True, 'b2': True} for r in seen), seen)
        self.assertIsNone(self.lock_of('b'))
        self.assertIsNone(self.lock_of('b2'))

    def test_import_from_locked_version_refused(self):
        c1 = self.get('b2')
        acquire_lock('b2', 'a merge')
        with self.assertRaises(VersionLockedError):
            vc.import_document(Component, c1.original_id, 'b2', 'b')
        self.assertEqual(self.get('b').mass.m, 1)


class TestAcquireRelease(LockTestCase):
    def test_acquire_stores_lock(self):
        token = acquire_lock('b', 'testing', user='me',
                             ttl=datetime.timedelta(minutes=1))
        lock = get_record('b').lock
        self.assertEqual((lock.token, lock.reason, lock.user),
                         (token, 'testing', 'me'))
        self.assertGreater(lock.expires, record_module.now())

    def test_release_semantics(self):
        self.assertFalse(release_lock('b'))
        token = acquire_lock('b', 'x')
        self.assertFalse(release_lock('b', ObjectId()))
        self.assertTrue(release_lock('b', token))
        self.assertFalse(release_lock('b', token))
        self.assertFalse(release_lock('nosuchversion'))
        acquire_lock('b', 'x')
        self.assertTrue(release_lock('b'))

    def test_acquire_missing_version(self):
        with self.assertRaises(KeyError):
            acquire_lock('nosuchversion', 'x')

    def test_acquire_is_exclusive_across_threads(self):
        results = []
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            try:
                results.append(acquire_lock('b', 'race'))
            except VersionLockedError:
                results.append(None)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(get_record('b').lock.token, winners[0])
