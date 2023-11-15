import unittest
from mongoengine import connect, disconnect, StringField, ReferenceField
import mongomock
from bgexplorer.models.verdoc import *
unittest.TestLoader.sortTestMethodsUsing = None

class A(VersionedDocument):
    name = StringField()

class B(VersionedDocument):
    name = StringField()
    a = VersionedReferenceField(A)

class TestVersionedDocumentv3(unittest.TestCase):
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
        A.drop_collection()
        B.drop_collection()

    def tearDown(self):
        if False:
            A.drop_collection()
            B.drop_collection()

    def test1_creation(self):
        a1 = A(name="a1").save()
        a2 = A.objects.get()
        self.assertEqual(a1.name, a2.name)
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(a2.original_id, a2.id)

    def test2_reference(self):
        a1 = A(name="a1").save()
        b1 = B(name="b1")
        b1 = B(name="b1", a=a1).save()
        b2 = B.objects.get()
        self.assertEqual(b2.a.id, a1.id)

    def test3_selecttag(self):
        a1 = A(name="a1").save()
        self.assertEqual(A.objects.select_tag('').count(), 1)
        a2 = A(name="a2", version_tags=['v2']).save()
        self.assertEqual(A.objects.select_tag('v2').count(), 1)

    def test4_createversion(self):
        A(name="a1").save()
        A(name="a2").save()
        A.create_tag('v1')
        self.assertEqual(A.objects.select_tag('').count(), 2)
        self.assertEqual(A.objects.select_tag('v1').count(), 2)
        a1 = A.objects.select_tag('').get(name="a1")
        a1v1 = A.objects.select_tag('v1').get(name="a1")
        self.assertEqual(a1.active_version, '')
        self.assertEqual(a1v1.active_version, 'v1')
        self.assertEqual(a1.id, a1v1.original_id)

    def test5_versionskept(self):
        a1 = A(name="a1").save()
        A.create_tag("v1")
        a1 = A.select_tag('').get(id=a1.id)
        a1.name = "a1 changed"
        a1.save()
        self.assertEqual(A.objects.count(), 2)
        self.assertEqual(A.select_tag('v1').get(original_id=a1.original_id).name, "a1")
        self.assertEqual(A.select_tag('').get(original_id=a1.original_id).name, "a1 changed")

    def test6_versionref(self):
        a1 = A(name="a1").save()
        b1 = B(name="b1", a=a1).save()
        A.create_tag('v1')
        B.create_tag('v1')
        self.assertEqual(B.select_tag('v1').get().a.active_version, 'v1', 'b1v1 referencing wrong tag')
        self.assertEqual(B.select_tag('v1').get().a.original_id, a1.id)



if __name__ == '__main__':
    unittest.main()
