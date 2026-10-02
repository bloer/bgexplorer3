""" Importing a single document from another version """
import unittest
from mongoengine import disconnect
from bson import ObjectId
from bgexplorer.models import versioncontrol as vc
from bgexplorer.models.versioncontrol import (_versioned_classes,
                                              import_document,
                                              VersionControlError)
from bgexplorer.models.verdoc import ReadOnlyVersionError, VersionLockedError
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.sourceterm import SourceTerm
from bgexplorer.models.settings import (VersionSettings, acquire_lock,
                                        release_lock, get_settings)
from bgexplorer.models.history import VersionEvent, EventAction
from tests.dbutil import connect_test_db
from tests.test_app_components import AppTestCase


def make_model():
    vc.create_version('main')
    e1 = EmissionSpec(name='e1', sources=[
        EmissionSource(name='K40', rate='1 mBq/kg')]).save()
    c1 = Component(name='c1', mass='1 kg', specs=[e1]).save()
    Assembly(name='a1', children=[Placement(component=c1)]).save()
    vc.create_version('b', 'main')
    return e1, c1


class TestImportDocument(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in [VersionSettings, VersionEvent] + _versioned_classes:
            cls.drop_collection()
        self.e1, self.c1 = make_model()

    def get(self, cls, name, tag):
        return cls.select_version(tag)(name=name).first()

    def test_import_new(self):
        e2 = EmissionSpec(name='e2', version_tag='b', sources=[
            EmissionSource(name='Co60', rate='1 mBq/kg')]).save()
        doc = import_document(EmissionSpec, e2.original_id, 'b', 'main')
        self.assertEqual(doc.active_version, 'main')
        e2.reload()
        self.assertEqual(sorted(e2.version_tags), ['b', 'main'])
        event = VersionEvent.objects.first()
        self.assertEqual((event.action, event.version, event.other_version),
                         (EventAction.import_document, 'main', 'b'))

    def test_import_replaces(self):
        c1 = self.get(Component, 'c1', 'b')
        c1.mass = '3 kg'
        c1.save()
        import_document(Component, c1.original_id, 'b', 'main')
        main = self.get(Component, 'c1', 'main')
        self.assertEqual(main.id, c1.id)
        self.assertEqual(main.mass.m, 3)
        # the replaced copy was only in main, so it is gone
        self.assertEqual(Component.objects(original_id=c1.original_id)
                         .count(), 1)
        # main's SourceTerms were regenerated for the new copy
        self.assertEqual(
            SourceTerm.select_version('main')(assemblyRoot=main).count(), 1)

    def test_replace_shared_copy(self):
        """ a copy shared with a third version stays in it """
        vc.create_version('c', 'main')
        c1 = self.get(Component, 'c1', 'b')
        c1.mass = '3 kg'
        c1.save()
        import_document(Component, c1.original_id, 'b', 'main')
        self.assertEqual(self.get(Component, 'c1', 'c').mass.m, 1)
        self.assertEqual(self.get(Component, 'c1', 'main').mass.m, 3)

    def test_sourceterms_follow_specs(self):
        e1 = self.get(EmissionSpec, 'e1', 'b')
        e1.sources.append(EmissionSource(name='Co60', rate='2 mBq/kg'))
        e1.save()
        self.assertEqual(
            SourceTerm.select_version('main')(assemblyRoot=self.c1).count(),
            1)
        import_document(EmissionSpec, e1.original_id, 'b', 'main')
        names = sorted(SourceTerm.select_version('main')(
            assemblyRoot=self.c1).scalar('source__name'))
        self.assertEqual(names, ['Co60', 'K40'])

    def test_dangling_refs(self):
        e2 = EmissionSpec(name='e2', version_tag='b').save()
        c1 = self.get(Component, 'c1', 'b')
        c1.specs.append(e2)
        c1.save()
        with self.assertRaises(VersionControlError) as cm:
            import_document(Component, c1.original_id, 'b', 'main')
        self.assertEqual(cm.exception.problems, [str(e2.original_id)])
        # nothing changed, and the lock was released
        self.assertEqual(len(self.get(Component, 'c1', 'main').specs), 1)
        self.assertIsNone(get_settings('main').lock)

    def test_refused(self):
        vc.create_tag('t', 'b')
        cid = self.c1.original_id
        with self.assertRaises(ReadOnlyVersionError):
            import_document(Component, cid, 'b', 't')
        token = acquire_lock('main', 'test')
        with self.assertRaises(VersionLockedError):
            import_document(Component, cid, 'b', 'main')
        release_lock('main', token)
        with self.assertRaises(VersionControlError):
            import_document(Component, ObjectId(), 'b', 'main')
        with self.assertRaises(VersionControlError):
            import_document(Component, cid, 'b', 'b')
        with self.assertRaises(KeyError):
            import_document(Component, cid, 'nosuch', 'b')


class TestImportPages(AppTestCase):
    def setUp(self):
        super().setUp()
        self.e1, self.c1 = make_model()

    def test_import(self):
        c1 = Component.select_version('b').get(name='c1')
        c1.mass = '3 kg'
        c1.save()
        diff = self.url('component.diff', 'main',
                        itemid=str(c1.original_id), other_version='b')
        html = self.html(self.client.get(diff))
        self.assertIn('id="importform"', html)
        url = self.url('component.import_from', 'main',
                       itemid=str(c1.original_id), from_version='b')
        self.assertIn(url, html)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Component.select_version('main').get(name='c1')
                         .mass.m, 3)
        # now they share a copy, so there's nothing to import
        self.assertNotIn('id="importform"',
                         self.html(self.client.get(diff)))

    def test_import_errors(self):
        e2 = EmissionSpec(name='e2', version_tag='b').save()
        c1 = Component.select_version('b').get(name='c1')
        c1.specs.append(e2)
        c1.save()
        url = self.url('component.import_from', 'main',
                       itemid=str(c1.original_id), from_version='b')
        response = self.client.post(url, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        html = self.html(response)
        self.assertIn("refers to documents that", html)
        self.assertIn('<li>e2</li>', html)
        vc.create_tag('t', 'b')
        url = self.url('component.import_from', 't',
                       itemid=str(c1.original_id), from_version='b')
        self.assertEqual(self.client.post(url).status_code, 403)
        url = self.url('component.import_from', 'main',
                       itemid='bad', from_version='b')
        self.assertEqual(self.client.post(url).status_code, 404)
