""" Splitting a shared document off for an edit never loses it, and an edit
that is interrupted can be recovered """
import datetime
import unittest
from unittest import mock
from bson import ObjectId
from mongoengine import disconnect
from bgexplorer.models import maintenance
from bgexplorer.models import versioncontrol as vc
from bgexplorer.models.component import Component
from bgexplorer.models.settings import VersionSettings
from bgexplorer.models.history import VersionEvent
from bgexplorer.models.verdoc import (versioned_classes, new_staging_tag,
                                      parse_staging_tag, VersionedQuerySet)
from tests.dbutil import connect_test_db


class TestStagingTag(unittest.TestCase):
    def test_roundtrip(self):
        tag = new_staging_tag('main')
        version, created = parse_staging_tag(tag)
        self.assertEqual(version, 'main')
        age = datetime.datetime.now(datetime.timezone.utc).replace(
            tzinfo=None) - created
        self.assertLess(abs(age.total_seconds()), 5)
        self.assertIsNone(vc.VERSION_NAME_PATTERN.fullmatch(tag))

    def test_other_tags(self):
        for tag in ('main', 'editing/', 'editing/main', 'editing/a/nonsense'):
            self.assertIsNone(parse_staging_tag(tag), tag)


class PrepareEditCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in [VersionSettings, VersionEvent] + versioned_classes():
            cls.drop_collection()
        vc.create_version('a')
        Component(name='c1', mass='1 kg', version_tag='a').save()
        Component(name='c2', mass='1 kg', version_tag='a').save()
        vc.create_version('b', 'a')
        self.coll = Component._get_collection()

    def copies(self, name):
        return list(self.coll.find({'name': name}, {'version_tags': 1}))

    def tags(self, name):
        return sorted(sorted(d['version_tags']) for d in self.copies(name))

    def edit(self, tag='b', name='c1'):
        return Component.select_version(tag)(name=name).update(
            set__description='edited')


class TestPrepareEdit(PrepareEditCase):
    def test_splits_shared_document(self):
        self.assertEqual(self.tags('c1'), [['a', 'b']])
        self.edit()
        self.assertEqual(self.tags('c1'), [['a'], ['b']])
        self.assertEqual(
            Component.select_version('b').get(name='c1').description,
            'edited')
        self.assertIsNone(
            Component.select_version('a').get(name='c1').description)
        # the other document is still shared
        self.assertEqual(self.tags('c2'), [['a', 'b']])

    def test_unshared_document_is_not_copied(self):
        self.edit()
        before = {d['_id'] for d in self.copies('c1')}
        self.edit()
        self.assertEqual({d['_id'] for d in self.copies('c1')}, before)

    def test_save_splits_too(self):
        c1 = Component.select_version('b').get(name='c1')
        c1.description = 'saved'
        c1.save()
        self.assertEqual(self.tags('c1'), [['a'], ['b']])
        self.assertIsNone(
            Component.select_version('a').get(name='c1').description)

    def test_no_staged_copies_left(self):
        self.edit()
        self.assertEqual(self.coll.count_documents(
            {'version_tags': {'$regex': '^editing/'}}), 0)

    def test_failure_after_copy_loses_nothing(self):
        real = VersionedQuerySet.update

        def fail_on_pull(qs, *args, **kwargs):
            if 'pull__version_tags' in kwargs:
                raise RuntimeError('interrupted')
            return real(qs, *args, **kwargs)
        with mock.patch.object(VersionedQuerySet, 'update', fail_on_pull):
            with self.assertRaises(RuntimeError):
                self.edit()
        # both versions still have the original; the copy is invisible
        self.assertEqual(Component.select_version('a').count(), 2)
        self.assertEqual(Component.select_version('b').count(), 2)
        self.assertEqual(len(self.copies('c1')), 2)
        self.assertEqual(Component.select_version('b').get(name='c1').id,
                         Component.select_version('a').get(name='c1').id)

    def test_failure_before_activation_keeps_the_item(self):
        real = VersionedQuerySet.update

        def fail_on_activate(qs, *args, **kwargs):
            if 'set__version_tags' in kwargs:
                raise RuntimeError('interrupted')
            return real(qs, *args, **kwargs)
        with mock.patch.object(VersionedQuerySet, 'update',
                               fail_on_activate):
            with self.assertRaises(RuntimeError):
                self.edit()
        # b has lost c1 for the moment, but the copy is there to restore
        self.assertEqual(Component.select_version('b').count(), 1)
        staged = list(self.coll.find({'version_tags': {'$regex': '^editing/'}}))
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0]['name'], 'c1')


class TestRecovery(PrepareEditCase):
    def interrupt(self, step):
        """ Run an edit of c1 in b that fails at `step` """
        real = VersionedQuerySet.update
        key = {'pull': 'pull__version_tags',
               'activate': 'set__version_tags'}[step]

        def fail(qs, *args, **kwargs):
            if key in kwargs:
                raise RuntimeError('interrupted')
            return real(qs, *args, **kwargs)
        with mock.patch.object(VersionedQuerySet, 'update', fail):
            with self.assertRaises(RuntimeError):
                self.edit()

    def test_recent_edits_are_left_alone(self):
        self.interrupt('activate')
        self.assertFalse(maintenance.find_orphans())
        self.assertEqual(maintenance.recover_interrupted_edits(),
                         dict(restored=0, discarded=0))
        self.assertEqual(Component.select_version('b').count(), 1)

    def test_restored_when_the_item_left_its_version(self):
        self.interrupt('activate')
        old = datetime.timedelta(0)
        with mock.patch.object(maintenance, 'EDIT_GRACE', old):
            orphans = maintenance.find_orphans()
            self.assertEqual(list(orphans.interrupted), ['Component'])
            self.assertTrue(orphans)
            self.assertEqual(orphans.unknown_tags, [])
            self.assertEqual(maintenance.recover_interrupted_edits(),
                             dict(restored=1, discarded=0))
        self.assertEqual(self.tags('c1'), [['a'], ['b']])
        self.assertEqual(Component.select_version('b').count(), 2)
        self.assertFalse(maintenance.find_orphans())

    def test_discarded_when_the_version_still_has_the_item(self):
        self.interrupt('pull')
        result = maintenance.recover_interrupted_edits(datetime.timedelta(0))
        self.assertEqual(result, dict(restored=0, discarded=1))
        self.assertEqual(self.tags('c1'), [['a', 'b']])

    def test_discarded_when_the_version_is_gone(self):
        self.interrupt('activate')
        VersionSettings.objects(version_tag='b').delete()
        result = maintenance.recover_interrupted_edits(datetime.timedelta(0))
        self.assertEqual(result, dict(restored=0, discarded=1))
        self.assertEqual(self.tags('c1'), [['a']])

    def test_delete_orphans_recovers(self):
        self.interrupt('activate')
        with mock.patch.object(maintenance, 'EDIT_GRACE',
                               datetime.timedelta(0)):
            self.assertEqual(maintenance.delete_orphans(), 1)
        self.assertEqual(self.tags('c1'), [['a'], ['b']])
        self.assertEqual(Component.select_version('b').count(), 2)


class TestDeleteScope(PrepareEditCase):
    def test_delete_only_removes_what_it_untagged(self):
        # an unrelated untagged document is not touched by a delete
        stray = ObjectId()
        self.coll.insert_one({'_id': stray, 'original_id': stray,
                              'name': 'stray', 'version_tags': [],
                              '_cls': 'Component'})
        Component.select_version('b')(name='c1').delete()
        self.assertIsNotNone(self.coll.find_one({'_id': stray}))
        self.assertEqual(self.tags('c1'), [['a']])
        # a document only in b is removed entirely
        Component(name='only', version_tag='b').save()
        Component.select_version('b')(name='only').delete()
        self.assertEqual(self.copies('only'), [])
