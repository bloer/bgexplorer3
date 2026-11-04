""" Creating, branching, deleting and editing versions of toy documents """
import unittest
from bgexplorer.versioning import (create_version, create_branch, create_tag,
                                   delete_version, list_versions,
                                   version_exists, get_record, hold_locks,
                                   ReadOnlyVersionError, VersionLockedError,
                                   ProtectedVersionError, diff_versions,
                                   compare_document, primary_classes,
                                   versioned_classes, maintenance)
from bgexplorer.versioning.locks import held_locks
from tests.versioning.toy import ToyTestCase


class TestRegistry(ToyTestCase):
    def test_registered(self):
        for cls in (self.Part, self.Group, self.Total):
            self.assertIn(cls, versioned_classes())

    def test_derived_not_primary(self):
        self.assertIn(self.Part, primary_classes())
        self.assertNotIn(self.Total, primary_classes())


class TestVersions(ToyTestCase):
    def test_branch_shares_documents(self):
        self.Part(name='bolt', weight=1.0).save()
        create_branch('dev', self.default_tag)
        self.assertTrue(version_exists('dev'))
        main = self.Part.select_version(self.default_tag).get()
        dev = self.Part.select_version('dev').get()
        self.assertEqual(main.id, dev.id)
        self.assertEqual(sorted(dev.version_tags),
                         sorted([self.default_tag, 'dev']))

    def test_edit_copies_on_write(self):
        self.Part(name='bolt', weight=1.0).save()
        create_branch('dev', self.default_tag)
        part = self.Part.select_version('dev').get()
        part.weight = 2.0
        part.save()
        main = self.Part.select_version(self.default_tag).get()
        dev = self.Part.select_version('dev').get()
        self.assertEqual(main.weight, 1.0)
        self.assertEqual(dev.weight, 2.0)
        self.assertNotEqual(main.id, dev.id)
        self.assertEqual(main.original_id, dev.original_id)
        self.assertEqual(self.Part.objects.count(), 2)

    def test_edit_unshared_does_not_copy(self):
        self.Part(name='bolt', weight=1.0).save()
        part = self.Part.select_version(self.default_tag).get()
        part.weight = 3.0
        part.save()
        self.assertEqual(self.Part.objects.count(), 1)

    def test_tag_is_read_only(self):
        self.Part(name='bolt').save()
        create_tag('v1', self.default_tag)
        self.assertFalse(get_record('v1').editable)
        part = self.Part.select_version('v1').get()
        part.weight = 5.0
        with self.assertRaises(ReadOnlyVersionError):
            part.save()

    def test_delete_keeps_shared(self):
        self.Part(name='bolt').save()
        create_branch('dev', self.default_tag)
        delete_version('dev')
        self.assertFalse(version_exists('dev'))
        self.assertEqual(self.Part.select_version(self.default_tag).count(), 1)
        self.assertEqual(self.Part.objects.count(), 1)

    def test_delete_removes_own(self):
        create_branch('dev', self.default_tag)
        self.Part.select_version('dev')
        part = self.Part(name='new')
        part.version_tags = ['dev']
        part.save()
        delete_version('dev')
        self.assertEqual(self.Part.objects.count(), 0)

    def test_default_version_protected(self):
        with self.assertRaises(ProtectedVersionError):
            delete_version(self.default_tag)

    def test_list_versions(self):
        create_branch('dev', self.default_tag)
        self.assertEqual({v.version_tag for v in list_versions()},
                         {self.default_tag, 'dev'})

    def test_duplicate_and_bad_names(self):
        with self.assertRaises(KeyError):
            create_version(self.default_tag)
        with self.assertRaises(ValueError):
            create_version('bad name!')


class TestLocks(ToyTestCase):
    def test_lock_blocks_other_writers(self):
        part = self.Part(name='bolt').save()
        with hold_locks([self.default_tag], 'testing'):
            # the holder may write
            part.weight = 1.0
            part.save()
            # code that doesn't hold the lock may not
            token = held_locks.set({})
            try:
                part.weight = 2.0
                with self.assertRaises(VersionLockedError):
                    part.save()
            finally:
                held_locks.reset(token)
        part.weight = 3.0
        part.save()

    def test_branch_of_locked_version_fails(self):
        with hold_locks([self.default_tag], 'testing'):
            token = held_locks.set({})
            try:
                with self.assertRaises(VersionLockedError):
                    create_branch('dev', self.default_tag)
            finally:
                held_locks.reset(token)
        self.assertFalse(version_exists('dev'))


class TestDiff(ToyTestCase):
    def test_diff_versions(self):
        self.Part(name='bolt', weight=1.0).save()
        self.Part(name='nut', weight=1.0).save()
        create_branch('dev', self.default_tag)
        bolt = self.Part.select_version('dev').get(name='bolt')
        bolt.weight = 2.0
        bolt.save()
        extra = self.Part(name='washer')
        extra.version_tags = ['dev']
        extra.save()
        result = diff_versions(self.default_tag, 'dev')
        parts = next(c for c in result.classes if c.cls is self.Part)
        self.assertEqual([i.name for i in parts.differ], ['part bolt'])
        self.assertEqual([i.name for i in parts.same], ['part nut'])
        self.assertEqual([i.name for i in parts.only_right], ['part washer'])
        self.assertTrue(result.changed)
        cmp = compare_document(self.Part, bolt.original_id,
                               self.default_tag, 'dev')
        self.assertEqual([e.pathstr for e in cmp.entries], ['weight'])


class TestHooks(ToyTestCase):
    def test_item_label_default_and_override(self):
        self.assertEqual(self.Part.item_label({'name': 'x'}), 'part x')
        self.assertEqual(self.Group.item_label({'name': 'x'}), 'x')

    def test_find_dangling(self):
        part = self.Part(name='bolt').save()
        group = self.Group(name='kit', parts=[part]).save()
        self.assertEqual(self.Group.find_dangling(self.default_tag), [])
        self.assertEqual(self.Part.find_dangling(self.default_tag), [])
        self.Part.objects(id=part.id).update(pull__version_tags=self.default_tag)
        self.Part.objects(id=part.id).update(push__version_tags='other')
        self.assertEqual(self.Group.find_dangling(self.default_tag),
                         [group.id])

    def test_find_orphans_reports_dangling(self):
        part = self.Part(name='bolt').save()
        group = self.Group(name='kit', parts=[part]).save()
        self.assertFalse(maintenance.find_orphans())
        self.Part.objects(id=part.id).update(
            set__version_tags=['other'])
        orphans = maintenance.find_orphans()
        self.assertEqual(orphans.dangling[self.default_tag],
                         {'Group': [group.id]})
        self.assertEqual(orphans.unknown_tags, ['other'])


if __name__ == '__main__':
    unittest.main()
