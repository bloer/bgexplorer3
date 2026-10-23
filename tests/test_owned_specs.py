""" EmissionSpecs that belong to a single component """
import unittest
from mongoengine import disconnect, ValidationError
from bgexplorer.models.component import (Component, Assembly, Placement,
                                         LocationMode)
from bgexplorer.models.emissionspec import EmissionSpec
from bgexplorer.models.exposure import RadonExposure, RadonPeriod
from bgexplorer.models.settings import VersionSettings
from bgexplorer.models.sourceterm import SourceTerm
from bgexplorer.models import versioncontrol as vc
from tests.dbutil import connect_test_db


class TestOwnedSpecs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in (VersionSettings, Component, EmissionSpec, SourceTerm):
            cls.drop_collection()
        vc.create_version('main')
        self.nominal = RadonExposure(name='1 day cleanroom', periods=[
            RadonPeriod(radonlevel='10 Bq/m**3', duration='1 day')]).save()
        self.c1 = Component(name='c1', outer_surface_area='1 m**2',
                            location='c1', specs=[self.nominal]).save()
        self.c2 = Component(name='c2', location='c2').save()

    def get(self, cls, name, version='main'):
        return cls.select_version(version).get(name=name)

    def test_add_owned_spec(self):
        own = self.c2.add_owned_spec(
            EmissionSpec(name='c2 assay', sources=dict(K40='1 mBq/kg')),
            confidence_rating=4, location='c2 assay spot',
            location_mode=LocationMode.suffix)
        c2 = self.get(Component, 'c2')
        self.assertEqual([s.name for s in c2.specs], ['c2 assay'])
        self.assertEqual(own.owner.name, 'c2')
        self.assertEqual((c2.spec_entries[0].confidence_rating,
                          c2.spec_entries[0].location,
                          c2.spec_entries[0].location_mode),
                         (4, 'c2 assay spot', LocationMode.suffix))
        self.assertEqual(SourceTerm.objects(assemblyRoot=c2).count(), 1)
        # it can't be attached to anything else
        c1 = self.get(Component, 'c1')
        c1.attach(own)
        with self.assertRaises(ValidationError):
            c1.save()
        with self.assertRaises(ValueError):
            Component(name='unsaved').add_owned_spec(EmissionSpec(name='x'))

    def test_owner_version(self):
        """ a new spec goes in its owner's version, not the default """
        vc.create_version('b', 'main')
        c2 = self.get(Component, 'c2', 'b')
        c2.add_owned_spec(EmissionSpec(name='in b',
                                       sources=dict(K40='1 mBq/kg')))
        self.assertEqual(EmissionSpec.objects.get(name='in b').version_tags,
                         ['b'])
        self.assertEqual(self.get(Component, 'c2', 'b').specs[0].name, 'in b')
        with self.assertRaises(ValueError):
            c2.add_owned_spec(self.nominal)

    def test_make_specific(self):
        c1 = self.get(Component, 'c1')
        entry = c1.find_spec_entry(self.nominal)
        entry.location = 'c1 surface'
        entry.confidence_rating = 2
        c1.save()
        own = c1.make_specific(self.nominal)
        self.assertEqual(own.name, '1 day cleanroom (c1)')
        self.assertIs(type(own), RadonExposure)
        c1 = self.get(Component, 'c1')
        self.assertEqual([s.original_id for s in c1.specs],
                         [own.original_id])
        # the settings follow the copy
        self.assertEqual(c1.spec_entries[0].location, 'c1 surface')
        self.assertEqual(c1.spec_entries[0].confidence_rating, 2)
        st = SourceTerm.objects(assemblyRoot=c1, source__name='Pb210').get()
        self.assertEqual(st.spec.original_id, own.original_id)
        self.assertEqual(st.location, 'c1 surface')
        # now its exposure can be tracked without changing the nominal one
        own.periods.append(RadonPeriod(radonlevel='50 Bq/m**3',
                                       duration='2 day'))
        own.save()
        self.assertEqual(len(self.get(EmissionSpec, '1 day cleanroom')
                             .periods), 1)

    def test_clone(self):
        c1 = self.get(Component, 'c1')
        own = c1.make_specific(self.nominal)
        c1 = self.get(Component, 'c1')
        c1.find_spec_entry(own).location = 'c1 surface'
        c1.save()
        copy = c1.clone(name='c1 copy')
        copy.save()
        copy = self.get(Component, 'c1 copy')
        spec = copy.specs[0]
        self.assertNotEqual(spec.original_id, own.original_id)
        self.assertEqual(spec.owner.original_id, copy.original_id)
        self.assertEqual(spec.name, own.name)
        self.assertEqual(copy.spec_entries[0].location, 'c1 surface')
        self.assertNotEqual(copy.spec_entries[0].id, c1.spec_entries[0].id)
        self.assertEqual(SourceTerm.objects(assemblyRoot=copy).count(), 1)

    def test_delete(self):
        """ deleting a component deletes its specs, only in that version """
        self.c2.add_owned_spec(EmissionSpec(name='c2 assay',
                                            sources=dict(K40='1 mBq/kg')))
        vc.create_version('b', 'main')
        self.get(Component, 'c2', 'b').delete()
        self.assertEqual(EmissionSpec.select_version('b')(name='c2 assay')
                         .count(), 0)
        self.assertEqual(EmissionSpec.select_version('main')(name='c2 assay')
                         .count(), 1)
        self.assertEqual(self.get(EmissionSpec, '1 day cleanroom', 'b').name,
                         '1 day cleanroom')


if __name__ == '__main__':
    unittest.main()
