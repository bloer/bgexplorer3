import unittest
from mongoengine import disconnect, ValidationError
from bgexplorer.models.cosmogenic import (ActivatedMaterial, CosmogenicIsotope,
                                          CosmogenicActivation,
                                          ActivationPeriod)
from bgexplorer.models.component import Component
from bgexplorer.models.emissionspec import (EmissionSpec, Multiplier,
                                            SourceCategory)
from bgexplorer.models.sourceterm import SourceTerm
from math import exp
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


def bqkg(q):
    m = q.to('Bq/kg').m
    return getattr(m, 'nominal_value', m)


class TestCosmogenicActivation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in (ActivatedMaterial, VersionSettings, Component,
                    EmissionSpec, SourceTerm):
            cls.drop_collection()
        vc.create_version('main')
        self.co60 = CosmogenicIsotope(isotope='Co60',
                                      activationrate='97 +- 10 1/kg/day')
        self.rate = 97 * units('1/kg/day')
        self.tau = get_tau('Co60')

    def activity(self, *periods):
        return self.co60.activity([ActivationPeriod(duration=d, factor=f)
                                   for d, f in periods])

    def test_buildup(self):
        t = 100 * units.day
        decayed = 1 - exp(-(t / self.tau).to('').m)
        self.assertAlmostEqual(bqkg(self.activity((t, 1))),
                               bqkg(self.rate * decayed))
        # saturates at the activation rate
        self.assertAlmostEqual(bqkg(self.activity(('1e6 day', 1))),
                               bqkg(self.rate))
        # cooling decays it
        cool = 365 * units.day
        self.assertAlmostEqual(
            bqkg(self.activity((t, 1), (cool, 0))),
            bqkg(self.activity((t, 1))) * exp(-(cool / self.tau).to('').m))
        # the factor scales the rate
        self.assertAlmostEqual(bqkg(self.activity(('0.5 day', 100))),
                               100 * bqkg(self.activity(('0.5 day', 1))))
        # uncertainty comes from the rate
        result = self.activity((t, 1)).to('Bq/kg').m
        self.assertAlmostEqual(result.s0 / result.nominal_value, 10 / 97)

    def test_periods_in_order(self):
        t1, t2, t3 = (30 * units.day, 1 * units.day, 200 * units.day)
        f = [1 - exp(-(t / self.tau).to('').m) for t in (t1, t2, t3)]
        expected = ((self.rate * f[0] * (1 - f[1]) + 50 * self.rate * f[1])
                    * (1 - f[2]))
        self.assertAlmostEqual(
            bqkg(self.activity((t1, 1), (t2, 50), (t3, 0))), bqkg(expected))

    def test_sources(self):
        cu = copper().save()
        spec = CosmogenicActivation(name='Cu', material=cu, periods=[
            ActivationPeriod(duration='30 day')]).save()
        self.assertEqual(spec.category, SourceCategory.activation)
        self.assertEqual([(s.name, s.multiplier, s.category)
                          for s in spec.sources],
                         [('Co60', Multiplier.mass, SourceCategory.activation),
                          ('Mn54', Multiplier.mass,
                           SourceCategory.activation)])
        ids = [s.id for s in spec.sources]
        c1 = Component(name='c1', mass='2 kg', specs=[spec]).save()

        # adding an isotope to the material adds a source; the others keep
        # their ids
        cu.isotopes.append(CosmogenicIsotope(isotope='Co57',
                                             activationrate='1 1/kg/day'))
        cu.save()
        spec = CosmogenicActivation.objects.get(name='Cu')
        self.assertEqual([s.name for s in spec.sources],
                         ['Co60', 'Mn54', 'Co57'])
        self.assertEqual([s.id for s in spec.sources[:2]], ids)
        self.assertEqual(SourceTerm.objects(assemblyRoot=c1).count(), 3)

        # and removing one removes it
        cu.isotopes = cu.isotopes[:1]
        cu.save()
        spec = CosmogenicActivation.objects.get(name='Cu')
        self.assertEqual([s.name for s in spec.sources], ['Co60'])
        st = SourceTerm.objects(assemblyRoot=c1).get()
        self.assertAlmostEqual(st.emissionrate.to('Bq').m.nominal_value,
                               2 * bqkg(spec.sources[0].rate))

    def test_owned_copy(self):
        """ one part's own history doesn't change the shared one """
        cu = copper().save()
        shared = CosmogenicActivation(name='Cu nominal', material=cu,
                                      periods=[ActivationPeriod(
                                          duration='30 day')]).save()
        c1 = Component(name='c1', mass='1 kg', specs=[shared]).save()
        c2 = Component(name='c2', mass='1 kg', specs=[shared]).save()
        own = c1.make_specific(shared)
        own.periods.append(ActivationPeriod(description='flight',
                                            duration='0.5 day', factor=100))
        own.save()

        def co60(component):
            return SourceTerm.objects(assemblyRoot=component,
                                      source__name='Co60').get()\
                .emissionrate.to('Bq').m.nominal_value
        self.assertGreater(co60(c1), co60(c2))
        self.assertEqual(len(CosmogenicActivation.objects.get(
            name='Cu nominal').periods), 1)

    def test_errors(self):
        with self.assertRaises(ValidationError):
            CosmogenicActivation(name='no material').save()
