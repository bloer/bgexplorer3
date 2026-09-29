import unittest
from mongoengine import disconnect, ValidationError
from bgexplorer.models.cosmogenic import ActivatedMaterial, CosmogenicIsotope
from bgexplorer.models.settings import VersionSettings
from bgexplorer.models.isotope import get_tau
from bgexplorer.models.common import units
from bgexplorer.models import versioncontrol as vc
from tests.dbutil import connect_test_db


def copper(**kwargs):
    return ActivatedMaterial(name='Cu', material='copper', isotopes=[
        CosmogenicIsotope(isotope='Co60', activationrate='97 +- 10 1/kg/day'),
        CosmogenicIsotope(isotope='Mn54', activationrate='<30 1/kg/day'),
    ], **kwargs)


class TestActivatedMaterial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        ActivatedMaterial.drop_collection()
        VersionSettings.drop_collection()

    def tearDown(self):
        # other tests create the same versions
        self.setUp()

    def test_validate(self):
        material = copper()
        material.validate()
        iso = material.isotopes[0]
        self.assertEqual(iso.activationrate.u, units('1/kg/day').u)
        self.assertEqual(iso.tau, get_tau('Co60'))
        self.assertIsNot(iso.id, material.isotopes[1].id)

        # activity units are equivalent
        CosmogenicIsotope(isotope='Co60',
                          activationrate='1 uBq/kg').validate()
        for bad in ('Fe56', 'notanisotope'):
            with self.assertRaises(ValidationError):
                CosmogenicIsotope(isotope=bad,
                                  activationrate='1 1/kg/day').validate()
        with self.assertRaises(ValidationError):
            CosmogenicIsotope(isotope='Co60',
                              activationrate='1 1/day').validate()
        with self.assertRaises(ValidationError):
            CosmogenicIsotope(isotope='Co60').validate()

    def test_duplicate_isotopes(self):
        material = copper()
        material.isotopes.append(CosmogenicIsotope(
            isotope='60Co', activationrate='1 1/kg/day'))
        with self.assertRaises(ValidationError) as cm:
            material.validate()
        self.assertIn('more than once', str(cm.exception))

        material = copper()
        material.isotopes[1].id = material.isotopes[0].id
        with self.assertRaises(ValidationError):
            material.validate()

    def test_versions(self):
        vc.create_version('main')
        copper().save()
        vc.create_version('b', 'main')
        material = ActivatedMaterial.select_version('b').get()
        material.isotopes[0].activationrate = '50 +- 5 1/kg/day'
        material.save()
        main = ActivatedMaterial.select_version('main').get()
        self.assertAlmostEqual(main.isotopes[0].activationrate.m.mode, 97)
        material = ActivatedMaterial.select_version('b').get()
        self.assertAlmostEqual(material.isotopes[0].activationrate.m.mode, 50)
        self.assertIn('ActivatedMaterial', vc.version_summary('b'))
