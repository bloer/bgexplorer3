import unittest
from mongoengine import (connect, disconnect, StringField, ReferenceField,
                         EmbeddedDocument, CASCADE, NULLIFY, PULL)
import mongomock
from bgexplorer.models.verdoc import *

class A(VersionedDocument):
    name = StringField()

class B(VersionedDocument):
    name = StringField()
    a = VersionedReferenceField(A)

class C(VersionedDocument):
    name = StringField()
    a = VersionedReferenceField(A, reverse_delete_rule=CASCADE)

class C2(VersionedDocument):
    name = StringField()
    a = VersionedReferenceField(A, reverse_delete_rule=NULLIFY)

class C3(VersionedDocument):
    name = StringField()
    a = ListField(VersionedReferenceField(A, reverse_delete_rule=PULL))

class D(VersionedDocument):
    name = StringField()
    a = VersionedListField(VersionedReferenceField(A))

class Holder(EmbeddedDocument):
    a = VersionedReferenceField(A)

class E(VersionedDocument):
    name = StringField()
    holders = VersionedEmbeddedDocumentListField(Holder)



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
        self.default_tag = VersionedDocument.get_default_tag()
        A.drop_collection()
        B.drop_collection()
        C.drop_collection()
        C2.drop_collection()
        C3.drop_collection()
        D.drop_collection()
        E.drop_collection()


    def tearDown(self):
        if DROPONTEARDOWN:
            A.drop_collection()
            B.drop_collection()
            C.drop_collection()

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
        self.assertEqual(A.objects.select_tag(self.default_tag).count(), 1)
        a2 = A(name="a2", version_tags=['v2']).save()
        self.assertEqual(A.objects.select_tag('v2').count(), 1)

    def test4_createversion(self):
        A(name="a1").save()
        A(name="a2").save()
        A.create_tag('v1')
        self.assertEqual(A.objects.select_tag(self.default_tag).count(), 2)
        self.assertEqual(A.objects.select_tag('v1').count(), 2)
        a1 = A.objects.select_tag(self.default_tag).get(name="a1")
        a1v1 = A.objects.select_tag('v1').get(name="a1")
        self.assertEqual(a1.active_version, self.default_tag)
        self.assertEqual(a1v1.active_version, 'v1')
        self.assertEqual(a1.id, a1v1.original_id)

    def test4_delete(self):
        A(name="a1").save().delete()
        self.assertEqual(A.objects.count(), 0)

        A.drop_collection()
        A(name="a1").save()
        A.create_tag('v1')
        a1 = A.objects.get()
        a1.active_version = self.default_tag
        a1.delete()
        self.assertEqual(A.objects.count(), 1)
        self.assertEqual(A.objects.get().version_tags, ['v1'])

        A.drop_collection()
        A(name="a1").save()
        A.create_tag('v1')
        A.select_version(self.default_tag).delete()
        self.assertEqual(A.objects.count(), 1)
        self.assertEqual(A.objects.get().version_tags, ['v1'])

    def test5_savenewversion(self):
        a1 = A(name="a1").save()
        A.create_tag("v1")
        a1 = A.select_tag(self.default_tag).get(id=a1.id)
        a1.name = "a1 changed"
        a1.save()
        self.assertEqual(A.objects.count(), 2)
        self.assertEqual(A.select_tag('v1').get(original_id=a1.original_id).name, "a1")
        self.assertEqual(A.select_tag(self.default_tag).get(original_id=a1.original_id).name, "a1 changed")

    def test6_versionref(self):
        a1 = A(name="a1").save()
        b1 = B(name="b1", a=a1).save()
        A.create_tag('v1')
        B.create_tag('v1')
        self.assertEqual(B.select_tag('v1').get().a.active_version, 'v1', 'b1v1 referencing wrong tag')
        self.assertEqual(B.select_tag('v1').get().a.original_id, a1.id)

    def test7_update(self):
        a1 = A(name="a1").save()
        A.create_tag('v1')
        A.select_tag(A.get_default_tag())(original_id=a1.original_id).update(name="a1 changed")
        self.assertEqual(A.objects.count(), 2)
        self.assertEqual(A.select_tag('v1').get().name, 'a1')
        self.assertEqual(A.select_tag(A.get_default_tag()).get().name, 'a1 changed')

    def test7_modify(self):
        a1 = A(name="a1").save()
        A.create_tag('v1')
        a1 = A.select_tag(self.default_tag).get()
        a1.modify(set__name="a2")
        self.assertEqual(A.objects.count(), 2)
        self.assertEqual(A.select_tag('v1').get().name, 'a1')
        self.assertEqual(A.select_tag(self.default_tag).get().name, 'a2')

    def test8_reverse_delete(self):
        """ Test that reverse delete rules work correctly and don't mess up
        other versions
        """
        a1 = A(name="a1").save()
        c1 = C(name="c1", a=a1).save()
        self.assertEqual(C.objects.count(), 1)
        a1.delete()
        self.assertEqual(C.objects.count(), 0)

        a1 = A(name="a1").save()
        c1 = C(name="c1", a=a1).save()
        A.create_tag('v1')
        C.create_tag('v1')
        self.assertEqual(C.objects.count(), 1)
        self.assertEqual(C.objects.get().version_tags, [self.default_tag, 'v1'])
        A.select_version(self.default_tag).delete()
        self.assertEqual(A.objects.count(), 1)
        self.assertEqual(C.objects.count(), 1)
        self.assertEqual(C.objects.get().version_tags, ['v1'])

    def test8_reverse_nullify(self):
        a1 = A(name="a1").save()
        c1 = C2(name="c1", a=a1).save()
        self.assertEqual(C2.objects.count(), 1)
        a1.delete()
        self.assertEqual(C2.objects.count(), 1)
        self.assertIsNone(C2.objects.get().a)

        C2.drop_collection()
        a1 = A(name="a1").save()
        c1 = C2(name="c1", a=a1).save()
        A.create_tag('v1')
        C2.create_tag('v1')
        self.assertEqual(C2.objects.count(), 1)
        self.assertEqual(C2.objects.get().version_tags, [self.default_tag, 'v1'])
        A.select_version(self.default_tag).delete()
        self.assertEqual(A.objects.count(), 1)
        self.assertEqual(C2.objects.count(), 2)
        self.assertEqual(C2.select_version('v1').get().a.id, a1.id)
        self.assertIsNone(C2.select_version(self.default_tag).get().a)

    def test8_reverse_pull(self):
        a1 = A(name="a1").save()
        c1 = C3(name="c1", a=[a1]).save()
        self.assertEqual(C3.objects.count(), 1)
        a1.delete()
        self.assertEqual(C3.objects.count(), 1)
        self.assertEqual(len(C3.objects.get().a), 0)

        C3.drop_collection()
        a1 = A(name="a1").save()
        c1 = C3(name="c1", a=[a1]).save()
        A.create_tag('v1')
        C3.create_tag('v1')
        self.assertEqual(C3.objects.count(), 1)
        self.assertEqual(C3.objects.get().version_tags, [self.default_tag, 'v1'])
        A.select_version(self.default_tag).delete()
        self.assertEqual(A.objects.count(), 1)
        self.assertEqual(C3.objects.count(), 2)
        self.assertEqual(C3.select_version('v1').get().a[0].id, a1.id)
        self.assertEqual(len(C3.select_version(self.default_tag).get().a), 0)

    def test9_update_reverse_delete(self):
        a1 = A(name="a1").save()
        c1 = C(name="c1", a=a1).save()
        a1.update(pull__version_tags=self.default_tag)
        self.assertEqual(A.objects.count(), 0)
        self.assertEqual(C.objects.count(), 0)

    def _edit_a1(self, a1):
        """ Tag everything as v1, then edit a1 in the default tag so that
        the two versions are stored in separate documents
        """
        for cls in (A, C3, D, E):
            cls.create_tag('v1')
        a1 = A.select_tag(self.default_tag).get(original_id=a1.original_id)
        a1.name = "a1 changed"
        a1.save()
        self.assertEqual(A.objects.count(), 2)

    def test10_versionref_list(self):
        a1 = A(name="a1").save()
        D(name="d1", a=[a1]).save()
        self._edit_a1(a1)
        self.assertEqual(D.objects.count(), 1)
        self.assertEqual(D.select_tag('v1').get().a[0].name, "a1")
        self.assertEqual(D.select_tag(self.default_tag).get().a[0].name,
                         "a1 changed")

    def test10_versionref_embedded_list(self):
        a1 = A(name="a1").save()
        E(name="e1", holders=[Holder(a=a1)]).save()
        self._edit_a1(a1)
        self.assertEqual(E.objects.count(), 1)
        self.assertEqual(E.select_tag('v1').get().holders[0].a.name, "a1")
        self.assertEqual(E.select_tag(self.default_tag).get().holders[0].a.name,
                         "a1 changed")

    def test10_embedded_active_version(self):
        a1 = A(name="a1").save()
        E(name="e1", holders=[Holder(a=a1)]).save()
        A.create_tag('v1')
        E.create_tag('v1')
        e1 = E.select_tag('v1').get()
        self.assertEqual(get_active_version(e1.holders[0]), 'v1')
        self.assertIsNone(get_active_version(Holder(a=a1)))



if __name__ == '__main__':
    unittest.main()
