""" Locked versions can only be changed by the lock holder, and operations on
versions are logged """
import unittest
from mongoengine import disconnect
from bson import ObjectId
from bgexplorer.models.versioncontrol import (create_version, create_tag,
                                              delete_version)
from bgexplorer.models.verdoc import VersionLockedError
from bgexplorer.models.component import Component
from bgexplorer.models.settings import (VersionSettings, get_settings,
                                        acquire_lock, release_lock,
                                        hold_locks)
from bgexplorer.models.history import (VersionEvent, EventAction,
                                       recent_events)
from tests.dbutil import connect_test_db
from tests.test_app_components import AppTestCase


class TestVersionLock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in (VersionSettings, Component, VersionEvent):
            cls.drop_collection()
        create_version('b')
        Component(name='c1', mass='1 kg', version_tag='b').save()
        create_version('b2', 'b')

    def get(self, tag='b'):
        return Component.objects.get(version_tags=tag, name='c1')

    def test_lock_blocks_writes(self):
        token = acquire_lock('b', 'test')
        c1 = self.get()
        c1.mass = '2 kg'
        with self.assertRaises(VersionLockedError):
            c1.save()
        with self.assertRaises(VersionLockedError):
            Component.select_version('b').update(set__mass='3 kg')
        with self.assertRaises(VersionLockedError):
            self.get().delete()
        with self.assertRaises(VersionLockedError):
            Component(name='c2', version_tag='b').save()
        with self.assertRaises(VersionLockedError):
            delete_version('b')
        # other versions are unaffected
        c1 = self.get('b2')
        c1.mass = '2 kg'
        c1.save()
        self.assertEqual(self.get().mass.m, 1)
        self.assertTrue(release_lock('b', token))
        self.assertIsNone(get_settings('b').lock)
        c1 = self.get()
        c1.mass = '4 kg'
        c1.save()

    def test_second_acquire_fails(self):
        token = acquire_lock('b', 'first')
        with self.assertRaises(VersionLockedError) as cm:
            acquire_lock('b', 'second')
        self.assertIn('first', str(cm.exception))
        # a wrong token doesn't release it
        self.assertFalse(release_lock("b", ObjectId()))
        self.assertTrue(release_lock('b', token))
        with self.assertRaises(KeyError):
            acquire_lock('nosuchversion', 'test')

    def test_holder_can_write(self):
        with hold_locks(['b', 'b2'], 'test'):
            self.assertIsNotNone(get_settings('b').lock)
            c1 = self.get()
            c1.mass = '2 kg'
            c1.save()
        self.assertIsNone(get_settings('b').lock)
        self.assertIsNone(get_settings('b2').lock)
        self.assertEqual(self.get().mass.m, 2)

    def test_hold_releases_on_failure(self):
        acquire_lock('b2', 'other')
        with self.assertRaises(VersionLockedError):
            with hold_locks(['b', 'b2'], 'test'):
                pass
        # the lock on 'b' was taken first, and released again
        self.assertIsNone(get_settings('b').lock)
        with self.assertRaises(RuntimeError):
            with hold_locks(['b'], 'test'):
                raise RuntimeError()
        self.assertIsNone(get_settings('b').lock)

    def test_branch_doesnt_copy_lock(self):
        with hold_locks(['b'], 'test'):
            create_version('b3', 'b')
        self.assertIsNone(get_settings('b3').lock)

    def test_events(self):
        create_tag('t', 'b')
        delete_version('b2')
        actions = [(e.action, e.version, e.other_version)
                   for e in recent_events('b')]
        self.assertEqual(actions, [
            (EventAction.create_tag, 't', 'b'),
            (EventAction.create_branch, 'b2', 'b'),
            (EventAction.create_branch, 'b', None),
            ])
        self.assertEqual(recent_events('b2')[0].action, EventAction.delete)
        self.assertEqual(recent_events('t')[0].summary,
                         "Tag t created from b")


class TestLockPages(AppTestCase):
    def setUp(self):
        super().setUp()
        VersionEvent.drop_collection()
        create_version('main')
        self.c1 = Component(name='c1', mass='1 kg').save()
        create_version('b', 'main')

    def test_history(self):
        html = self.html(self.client.get(self.url('overview', 'main')))
        self.assertIn('id="history"', html)
        self.assertIn('Branch b created from main', html)

    def test_locked_pages(self):
        acquire_lock('b', 'a merge')
        html = self.html(self.client.get(self.url('overview', 'b')))
        self.assertIn('id="lockwarning"', html)
        self.assertIn('locked by a merge', html)
        # editing is refused while locked
        url = self.url('component.edit', 'b', objid=self.c1.original_id)
        response = self.client.post(url, data={'name': 'c1', 'mass': '2 kg'})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(self.url('versions.unlock', 'b'))
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(get_settings('b').lock)
        self.assertEqual(recent_events('b')[0].action,
                         EventAction.clear_lock)
        response = self.client.post(url, data={'name': 'c1', 'mass': '2 kg'})
        self.assertEqual(response.status_code, 302)
