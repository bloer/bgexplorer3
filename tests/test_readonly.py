""" Read-only versions (tags) can't be modified """
import unittest
from mongoengine import disconnect
from bgexplorer.models.versioncontrol import (create_version, create_tag,
                                              create_branch, delete_version,
                                              version_exists)
from bgexplorer.models.verdoc import ReadOnlyVersionError
from bgexplorer.models.component import Component, Assembly
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.sourceterm import SourceTerm, CalculatedResults
from bgexplorer.models.settings import VersionSettings, get_settings
from tests.dbutil import connect_test_db


class TestReadOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in (VersionSettings, Component, EmissionSpec, SourceTerm,
                    CalculatedResults, HitEfficiency):
            cls.drop_collection()
        create_version('b')
        e1 = EmissionSpec(name='e1', version_tag='b',
                          sources=[EmissionSource(name='K40',
                                                  rate='1 mBq/kg')]).save()
        c1 = Component(name='c1', mass='1 kg', specs=[e1],
                       version_tag='b').save()
        Assembly(name='a1', version_tag='b',
                 components=[c1]).save()
        create_tag('t', 'b')

    def get(self, cls, name, tag='t'):
        return cls.objects.get(version_tags=tag, name=name)

    def test_save(self):
        c1 = self.get(Component, 'c1')
        c1.mass = '2 kg'
        with self.assertRaises(ReadOnlyVersionError):
            c1.save()
        # nothing changed in either version
        self.assertEqual(Component.objects.count(), 2)
        for tag in ('b', 't'):
            self.assertEqual(self.get(Component, 'c1', tag).mass.m, 1)
        # the branch is still editable
        c1 = self.get(Component, 'c1', 'b')
        c1.mass = '2 kg'
        c1.save()
        self.assertEqual(self.get(Component, 'c1', 't').mass.m, 1)

    def test_new_document(self):
        with self.assertRaises(ReadOnlyVersionError):
            Component(name='c2', version_tag='t').save()
        self.assertEqual(Component.select_version('t').count(), 2)

    def test_queryset(self):
        with self.assertRaises(ReadOnlyVersionError):
            Component.select_version('t').update(set__description='x')
        with self.assertRaises(ReadOnlyVersionError):
            Component.select_version('t')(name='c1')\
                .modify(set__description='x')
        with self.assertRaises(ReadOnlyVersionError):
            Component.select_version('t')(name='c1').delete()
        self.assertEqual(Component.select_version('t').count(), 2)
        self.assertIsNone(self.get(Component, 'c1').description)

    def test_document(self):
        e1 = self.get(EmissionSpec, 'e1')
        with self.assertRaises(ReadOnlyVersionError):
            e1.update(set__description='x')
        with self.assertRaises(ReadOnlyVersionError):
            e1.delete()
        # a document that belongs only to the tag is deleted by pk
        e1 = self.get(EmissionSpec, 'e1', 'b')
        e1.description = 'x'
        e1.save()
        e1t = self.get(EmissionSpec, 'e1')
        self.assertEqual(e1t.version_tags, ['t'])
        with self.assertRaises(ReadOnlyVersionError):
            e1t.delete()
        # one sourceterm for c1 on its own and one as part of a1
        self.assertEqual(SourceTerm.select_version('t').count(), 2)

    def test_settings(self):
        settings = get_settings('t', create=False)
        settings.description = 'changed'
        with self.assertRaises(ReadOnlyVersionError):
            settings.save()
        settings = get_settings('t', create=False)
        settings.editable = True
        with self.assertRaises(ReadOnlyVersionError):
            settings.save()
        self.assertFalse(get_settings('t', create=False).editable)
        # the branch's settings can change
        settings = get_settings('b', create=False)
        settings.description = 'changed'
        settings.save()

    def test_delete_tag(self):
        delete_version('t')
        self.assertFalse(version_exists('t'))
        self.assertEqual(Component.select_version('b').count(), 2)
        self.assertEqual(SourceTerm.select_version('b').count(), 2)
        self.assertEqual(Component.objects(version_tags='t').count(), 0)

    def test_branch_from_tag(self):
        create_branch('b2', 't')
        c1 = self.get(Component, 'c1', 'b2')
        c1.mass = '3 kg'
        c1.save()
        self.assertEqual(self.get(Component, 'c1').mass.m, 1)
        for st in SourceTerm.select_version('b2'):
            self.assertEqual(st.emissionrate.mode, 3)
        for st in SourceTerm.select_version('t'):
            self.assertEqual(st.emissionrate.mode, 1)
