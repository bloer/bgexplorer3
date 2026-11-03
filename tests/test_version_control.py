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


class TestDeleteVersion(unittest.TestCase):
    """ delete_version works on the raw collections, so check the result
    against every stored document rather than through the ORM
    """
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        VersionSettings.drop_collection()
        for cls in _versioned_classes:
            cls.drop_collection()

    @staticmethod
    def _raw():
        """ every stored document, by collection and id """
        return {cls.__name__: {d['_id']: d for d in cls._get_collection().find()}
                for cls in _versioned_classes}

    @staticmethod
    def _add(tag, name):
        """ one document of each kind of versioned class that we make by hand
        """
        Component(name=name, version_tag=tag).save()
        Assay(name=name, version_tag=tag).save()
        HitEfficiency(source=name, location=name, version_tag=tag).save()

    def _delete_and_check(self, tag):
        """ Delete `tag` and verify every collection. Returns how many
        documents were (deleted, untagged, untouched) so tests can make sure
        each case was actually exercised
        """
        before = self._raw()
        deleted = untagged = untouched = 0
        expected = {}
        for name, docs in before.items():
            expected[name] = {}
            for _id, doc in docs.items():
                tags = doc['version_tags']
                if tag not in tags:
                    expected[name][_id] = doc
                    untouched += 1
                elif tags == [tag]:
                    deleted += 1
                else:
                    kept = [t for t in tags if t != tag]
                    expected[name][_id] = {**doc, 'version_tags': kept}
                    untagged += 1

        delete_version(tag)

        # exactly the expected documents remain, and only version_tags changed
        self.assertEqual(self._raw(), expected)
        # nothing is left unversioned or still carrying the deleted tag
        for cls in _versioned_classes:
            coll = cls._get_collection()
            self.assertEqual(coll.count_documents(
                {'$or': [{'version_tags': {'$exists': False}},
                         {'version_tags': None},
                         {'version_tags': []}]}), 0, cls.__name__)
            self.assertEqual(coll.count_documents({'version_tags': tag}), 0,
                             cls.__name__)
        self.assertFalse(version_exists(tag))
        self.assertEqual(VersionSettings.objects(version_tag=tag).count(), 0)
        return deleted, untagged, untouched

    def test_exclusive_shared_unrelated(self):
        create_version('base')
        self._add('base', 'shared')
        create_version('branch', 'base')
        self._add('branch', 'branch_and_tag')
        create_tag('tag', 'branch')
        self._add('branch', 'branch_only')
        self._add('base', 'base_only')
        create_version('solo')
        self._add('solo', 'solo')
        self.assertEqual(Component.objects(name='shared').get().version_tags,
                         ['base', 'branch', 'tag'])

        deleted, untagged, untouched = self._delete_and_check('branch')
        # 3 kinds of documents each: branch_only deleted;
        # shared and branch_and_tag shared with others; the rest unrelated
        self.assertEqual((deleted, untagged, untouched), (3, 6, 6))

        self.assertEqual(Component.objects(name='shared').get().version_tags,
                         ['base', 'tag'])
        self.assertEqual(Component.objects(name='branch_and_tag').get()
                         .version_tags, ['tag'])
        self.assertEqual(Assay.objects(name='branch_and_tag').get()
                         .version_tags, ['tag'])
        self.assertEqual(HitEfficiency.objects(source='branch_and_tag').get()
                         .version_tags, ['tag'])
        self.assertEqual(Component.objects(name='base_only').get().version_tags,
                         ['base'])
        self.assertEqual(Component.objects(name='solo').get().version_tags,
                         ['solo'])
        for cls, field in ((Component, 'name'), (Assay, 'name'),
                           (HitEfficiency, 'source')):
            self.assertEqual(cls.objects(**{field: 'branch_only'}).count(), 0)
        self.assertEqual(sorted(v.version_tag for v in list_versions()),
                         ['base', 'solo', 'tag'])

    def test_delete_middle_of_history(self):
        """ the order of the remaining tags is preserved """
        create_version('a')
        self._add('a', 'x')
        create_version('b', 'a')
        create_version('c', 'b')
        self.assertEqual(Component.objects(name='x').get().version_tags,
                         ['a', 'b', 'c'])
        self.assertEqual(self._delete_and_check('b'), (0, 3, 0))
        self.assertEqual(Component.objects(name='x').get().version_tags,
                         ['a', 'c'])
        self.assertEqual(self._delete_and_check('a'), (0, 3, 0))
        self.assertEqual(Component.objects(name='x').get().version_tags, ['c'])

    def test_similar_tag_names(self):
        """ the match is on whole tags, not prefixes or substrings """
        for tag in ('v1', 'v10', 'v1.0', 'xv1', 'V1'):
            create_version(tag)
            self._add(tag, 'only_' + tag)
        # share two of the similarly named components with v1
        for tag in ('v10', 'xv1'):
            Component.objects(name='only_' + tag).update(
                add_to_set__version_tags='v1')
        deleted, untagged, untouched = self._delete_and_check('v1')
        self.assertEqual((deleted, untagged, untouched), (3, 2, 10))
        self.assertEqual(sorted(
            Component.objects.distinct('name')),
            ['only_V1', 'only_v1.0', 'only_v10', 'only_xv1'])
        self.assertEqual(Component.objects(name='only_v10').get().version_tags,
                         ['v10'])

    def test_empty_version(self):
        create_version('main_line')
        self._add('main_line', 'x')
        create_version('empty')
        self.assertEqual(self._delete_and_check('empty'), (0, 0, 3))

    def test_last_version(self):
        create_version('only')
        self._add('only', 'x')
        self._add('only', 'y')
        self.assertEqual(self._delete_and_check('only'), (6, 0, 0))
        for docs in self._raw().values():
            self.assertEqual(docs, {})
        self.assertEqual(VersionSettings.objects.count(), 0)

    def test_references(self):
        """ components and the specs they refer to, deleted without checking
        the references between them
        """
        create_version('base')
        shared_spec = Assay(name='shared_spec', version_tag='base').save()
        Component(name='shared_comp', mass='1 kg', specs=[shared_spec],
                  version_tag='base').save()
        create_version('branch', 'base')
        spec = Assay(name='spec', version_tag='branch').save()
        Component(name='comp', mass='1 kg', specs=[spec],
                  version_tag='branch').save()
        deleted, untagged, _ = self._delete_and_check('branch')
        self.assertGreaterEqual(deleted, 2)
        self.assertGreaterEqual(untagged, 2)
        comp = Component.select_version('base').get(name='shared_comp')
        self.assertEqual([s.name for s in comp.specs], ['shared_spec'])
        self.assertEqual(Component.objects(name='comp').count(), 0)
        self.assertEqual(Assay.objects(name='spec').count(), 0)

    def test_delete_every_version(self):
        create_version('a')
        self._add('a', 'in_a')
        create_version('b', 'a')
        self._add('b', 'in_b')
        create_tag('c', 'b')
        for tag in ('b', 'c', 'a'):
            self._delete_and_check(tag)
        for docs in self._raw().values():
            self.assertEqual(docs, {})
