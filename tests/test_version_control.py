import unittest
from mongoengine import *
from bgexplorer.models.versioncontrol import *
from bgexplorer.models.versioncontrol import _versioned_classes
from bgexplorer.models.component import Component
from bgexplorer.models.assay import Assay
from tests.dbutil import connect_test_db

DROPONTEARDOWN = False


class TestVersionedDocument(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        VersionSettings.drop_collection()
        # every versioned collection, so tags left by other tests don't clash
        for cls in _versioned_classes:
            cls.drop_collection()

    def tearDown(self):
        if DROPONTEARDOWN:
            VersionSettings.drop_collection()
            Component.drop_collection()
            Assay.drop_collection()

    def test1_createversion(self):
        create_version('test')
        settings = get_settings('test', create=False)
        self.assertEqual(settings.version_tag, 'test')

    def _make_branch(self):
        create_version('test')
        c1 = Component(name='c1', version_tag='test').save()
        a1 = Assay(name='a1', version_tag='test').save()
        create_version('branch', 'test')
        c1.reload()
        a1.reload()
        self.assertEqual(c1.version_tags, ['test', 'branch'])
        self.assertEqual(a1.version_tags, ['test', 'branch'])
        return (c1, a1)

    def test2_branch(self):
        self._make_branch()

    def test3_delete(self):
        c1, a1 = self._make_branch()
        delete_version('branch')
        c1.reload()
        a1.reload()
        self.assertEqual(c1.version_tags, ['test'])
        self.assertEqual(a1.version_tags, ['test'])
        delete_version('test')
        self.assertEqual(Component.objects.count(), 0)
        self.assertEqual(Assay.objects.count(), 0)

    def test8_create_options(self):
        create_version('test', description='source')
        c1 = Component(name='c1', version_tag='test').save()
        tag = create_tag('v1', 'test', description='a tag')
        self.assertFalse(tag.editable)
        self.assertEqual(tag.description, 'a tag')
        self.assertFalse(get_settings('v1', create=False).editable)
        # a branch of a tag is editable, and keeps the source description
        branch = create_branch('b1', 'v1')
        self.assertTrue(branch.editable)
        self.assertEqual(branch.description, 'a tag')
        self.assertEqual(Component.select_version('b1').count(), 1)
        # an empty tag
        self.assertFalse(create_tag('emptytag').editable)
        with self.assertRaises(KeyError):
            create_version('b2', 'missing')
        self.assertFalse(version_exists('missing'))
        self.assertFalse(version_exists('b2'))
        with self.assertRaises(KeyError):
            create_version('v1', 'test')

    def test8_names(self):
        for name in ('', ' ', ' a', 'a/', '/a', 'a/b', 'my api', '.a', '-a',
                     'a?', None):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    create_version(name)
        # names of top-level routes are fine: versions live under /explore
        for name in ('v1.0', 'api', 'versions', 'admin', 'a_b-c'):
            create_version(name)
        self.assertEqual([v.version_tag for v in list_versions()],
                         ['a_b-c', 'admin', 'api', 'v1.0', 'versions'])

    def test9_delete_protected(self):
        create_version('main')
        with self.assertRaises(ProtectedVersionError):
            delete_version('main')
        self.assertTrue(version_exists('main'))
        with self.assertRaises(KeyError):
            delete_version('missing')
        c1 = Component(name='c1', version_tag='main').save()
        create_tag('v1', 'main')
        with self.assertRaises(ProtectedVersionError):
            delete_version('v1', allow_tags=False)
        self.assertTrue(version_exists('v1'))
        with self.assertRaises(KeyError):
            delete_version('missing', allow_tags=False)
        create_version('b', 'main')
        delete_version('b', allow_tags=False)
        self.assertFalse(version_exists('b'))
        # the model still allows deleting tags by default
        delete_version('v1')
        self.assertFalse(version_exists('v1'))
        c1.reload()
        self.assertEqual(c1.version_tags, ['main'])

    def test9_other_versions(self):
        create_version('main')
        c1 = Component(name='c1', version_tag='main').save()
        create_tag('v1', 'main')
        create_version('b', 'main')
        c1b = Component.select_version('b').get(name='c1')
        c1b.description = 'edited'
        c1b.save()
        rows = c1b.other_versions()
        self.assertEqual([r['version'] for r in rows], ['b', 'main', 'v1'])
        self.assertEqual([r['current'] for r in rows], [True, False, False])
        self.assertEqual([r['same_copy'] for r in rows], [True, False, False])
        self.assertEqual(rows[0]['revision'], c1b.revision)
        self.assertNotEqual(rows[0]['revision'], rows[1]['revision'])
        rows = Component.select_version('v1').get(name='c1').other_versions()
        self.assertEqual([r['same_copy'] for r in rows], [False, True, True])
        self.assertEqual(Component(name='new').other_versions(), [])

    def test9_summary(self):
        create_version('test')
        Component(name='c1', version_tag='test').save()
        Assay(name='a1', version_tag='test').save()
        create_version('branch', 'test')
        Component(name='c2', version_tag='branch').save()
        summary = version_summary('branch')
        self.assertEqual(summary['Component'], {'total': 2, 'unique': 1})
        self.assertEqual(summary['EmissionSpec'], {'total': 1, 'unique': 0})
        self.assertEqual(summary['SourceTerm'], {'total': 0, 'unique': 0})
        with self.assertRaises(KeyError):
            version_summary('missing')
