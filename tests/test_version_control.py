import unittest
from mongoengine import *
from bgexplorer.models.versioncontrol import *
from bgexplorer.models.component import Component
from bgexplorer.models.assay import Assay

DROPONTEARDOWN = False


class TestVersionedDocument(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # TODO: try to use mongo, and if it fails, switch to monomock
        # and add an expected failure for all $merge pipelines
        connect(uuidRepresentation='standard')
        #connect('mongoenginetest', host='mongodb://localhost',
        #        mongo_client_class=mongomock.MongoClient,
        #        uuidRepresentation='stanard')

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        VersionSettings.drop_collection()
        Component.drop_collection()
        Assay.drop_collection()

    def tearDown(self):
        if DROPONTEARDOWN:
            VersionSettings.drop_collection()
            Component.drop_collection()
            Assay.drop_collection()

    def test1_createversion(self):
        create_version('test')
        settings = get_settings('test', create=False)
        self.assertEquals(settings.version_tag, 'test')

    def test2_branch(self):
        create_version('test')
        c1 = Component(name='c1', version_tag='test').save()
        a1 = Assay(name='a1', version_tag='test').save()
        create_version('branch', 'test')
        c1.reload()
        a1.reload()
        self.assertEqual(c1.version_tags, ['test', 'branch'])
        self.assertEqual(a1.version_tags, ['test', 'branch'])
        return (c1, a1)

    def test3_delete(self):
        c1, a1 = self.test2_branch()
        delete_version('branch')
        c1.reload()
        a1.reload()
        self.assertEqual(c1.version_tags, ['test'])
        self.assertEqual(a1.version_tags, ['test'])
        delete_version('test')
        self.assertEqual(Component.objects.count(), 0)
        self.assertEqual(Assay.objects.count(), 0)

    def test4_merge_replace_all(self):
        create_version('test')
        c1 = Component(name='c1', version_tag='test').save()
        a1 = Assay(name='a1', version_tag='test').save()
        create_version('branch', 'test')
        c2 = Component(name='c2', version_tag='test').save()
        c3 = Component(name='c3', version_tag='branch').save()
        merge_version('branch', 'test', method=MergeMethod.replace_all)
        self.assertEqual(Component.select_version('test').count(), 2)
        self.assertEqual(Component.objects(name='c2').count(), 0)
        c3.reload()
        self.assertEqual(c3.version_tags, ['branch', 'test'])

    def test5_merge_keep_original(self):
        create_version('test')
        c1 = Component(name='c1', version_tag='test').save()
        create_version('branch', 'test')
        c2 = Component(name='c2', version_tag='test').save()
        Component.objects(version_tags='test', name='c1').update(set__description='test')
        merge_version('branch', 'test', method=MergeMethod.keep_othertag)
        self.assertEqual(Component.objects.count(), 3)
        self.assertEqual(Component.select_version('test').count(), 2)
        c1vtest = Component.objects.get(version_tags='test', name='c1')
        self.assertEqual(c1vtest.description, 'test')
        c1vbranch = Component.objects.get(version_tags='branch', name='c1')
        self.assertIsNone(c1vbranch.description)

    def test6_merge_keep_thistag(self):
        create_version('test')
        c1 = Component(name='c1', version_tag='test').save()
        create_version('branch', 'test')
        c2 = Component(name='c2', version_tag='test').save()
        Component.objects(version_tags='test', name='c1').update(set__description='test')
        merge_version('branch', 'test', method=MergeMethod.keep_thistag)
        self.assertEqual(Component.objects.count(), 2)
        self.assertEqual(Component.select_version('test').count(), 2)
        c1vtest = Component.objects.get(version_tags='test', name='c1')
        self.assertIsNone(c1vtest.description)
        c1vbranch = Component.objects.get(version_tags='branch', name='c1')
        self.assertIsNone(c1vbranch.description)

    def test7_merge_keep_newest(self):
        create_version('test')
        c1 = Component(name='c1', version_tag='test').save()
        c2 = Component(name='c2', version_tag='test').save()
        create_version('branch', 'test')
        Component.objects(version_tags='test', name='c1').update(set__description='test')
        Component.objects(version_tags='branch', name='c2').update(set__description='test')
        self.assertEqual(Component.objects.count(), 4)
        merge_version('branch', 'test', method=MergeMethod.keep_newest)
        self.assertEqual(Component.objects.count(), 3)
        self.assertEqual(Component.select_version('test').count(), 2)
        self.assertEqual(Component.select_version('branch').count(), 2)
        self.assertEqual(Component.objects.get(version_tags='test', name='c1').description,
                         'test')
        self.assertEqual(Component.objects.get(version_tags='test', name='c2').description,
                         'test')
        self.assertIsNone(Component.objects.get(version_tags='branch', name='c1').description)
        self.assertEqual(Component.objects.get(version_tags='branch', name='c2').description,
                         'test')

