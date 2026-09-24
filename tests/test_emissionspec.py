import unittest
from mongoengine import connect, disconnect, Document, ValidationError
from bgexplorer.models.emissionspec import EmissionSource, EmissionSpec, Multiplier
from bgexplorer.models.common import units, DimensionalityError
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.isotope import concentration_to_rate
from tests.dbutil import connect_test_db

class TestEmission(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        EmissionSpec.drop_collection()

    def tearDown(self):
        pass

    def test1_findmultiplier(self):
        s = EmissionSource(name='U238', rate='10 +/- 1 Bq/kg')
        s.validate()
        self.assertEqual(s.multiplier, Multiplier.mass)

        s = EmissionSource(name='U238', rate='10 +/- 1 ppb')
        s.validate()
        self.assertEqual(s.multiplier, Multiplier.mass)

        s = EmissionSource(name='U238', rate='10 +/- 1 Bq/m**3')
        s.validate()
        self.assertEqual(s.multiplier, Multiplier.volume)

        s = EmissionSource(name='U238', rate='10 +/- 1 Bq/cm**2')
        s.validate()
        self.assertEqual(s.multiplier, Multiplier.surface)

        s = EmissionSource(name='U238', rate='10 +/- 1 1/s')
        s.validate()
        self.assertEqual(s.multiplier, Multiplier.none)

        s = EmissionSource(name='U238', rate='10 1/kg')
        with self.assertRaises(ValidationError):
            s.validate()

    def test2_checkunits(self):
        s = EmissionSource(name='', rate='10 Bq/kg')
        for mult in Multiplier:
            s.multiplier = mult
            if mult is Multiplier.mass:
                s.validate()
            else:
                with self.assertRaises(ValidationError):
                    s.validate()

        s = EmissionSource(name='', rate='10 Bq/m**3')
        for mult in Multiplier:
            s.multiplier = mult
            if mult is Multiplier.volume:
                s.validate()
            else:
                with self.assertRaises(ValidationError):
                    s.validate()

        s = EmissionSource(name='', rate='10 Bq/m**2')
        for mult in Multiplier:
            s.multiplier = mult
            if mult in (Multiplier.surface, Multiplier.inner_surface,
                        Multiplier.outer_surface, Multiplier.none):
                s.validate()
            else:
                with self.assertRaises(ValidationError):
                    s.validate()

        s = EmissionSource(name='', rate='10 Bq')
        for mult in Multiplier:
            s.multiplier = mult
            if mult is Multiplier.none:
                s.validate()
            else:
                with self.assertRaises(ValidationError):
                    s.validate()

    def test3_autogen(self):
        source = EmissionSource(name='U238', rate='10 +- 0.2 Bq/kg')
        spec = EmissionSpec(name='', sources=[source])
        spec.validate()
        self.assertEqual(len(spec.sources), 3)
        for newsource in spec.sources[1:]:
            self.assertIn(newsource.name, ['Ra226', 'U235'])
            self.assertEqual(newsource.generated_from, source.id)

        # make sure repeated validation doesn't insert multiple
        spec.validate()
        self.assertEqual(len(spec.sources), 3)

        spec.sources = spec.sources[1:]
        self.assertEqual(len(spec.sources), 2)
        spec.validate()
        self.assertEqual(len(spec.sources), 0)

    def test3_autogen_concentration(self):
        """ sources derived from a concentration have the right rate """
        source = EmissionSource(name='U238', rate='81 ppb')
        spec = EmissionSpec(name='', sources=[source])
        spec.validate()
        rates = {s.name: s.rate for s in spec.sources}
        self.assertEqual(set(rates), {'U238', 'U235', 'Ra226'})
        # secular equilibrium
        self.assertAlmostEqual(
            concentration_to_rate('Ra226', rates['Ra226']).to('Bq/kg').mode,
            concentration_to_rate('U238', rates['U238']).to('Bq/kg').mode)
        # relative abundance
        self.assertAlmostEqual(rates['U235'].to('ppb').mode, 81 * 0.00725)

    def test4_rateid(self):
        # test that emissionrate has an id assigned
        source = EmissionSource(name="U238", rate="10 +- 0.2 Bq/kg")
        self.assertEqual(source.rate.id, source.id)

    def test_json(self):
        source = EmissionSource(name='U238', rate='10 +- 0.2 Bq/kg')
        spec = EmissionSpec(name='', sources=[source])
        json = spec.to_json()
        spec2 = EmissionSpec.from_json(json)
        self.assertEqual(spec.id, spec2.id)
        self.assertEqual(spec.sources[0].id, spec2.sources[0].id)
        self.assertEqual(spec.sources[0].rate, spec2.sources[0].rate)
