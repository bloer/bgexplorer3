import unittest
from mongoengine import connect, disconnect, Document, ValidationError
from bgexplorer.models.emissionspec import EmissionSource, EmissionSpec, Multiplier
from bgexplorer.models.common import units, DimensionalityError
from bgexplorer.models.asymmetric import AsymmetricUncertainty

class TestEmission(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect(uuidRepresentation='standard')

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

    def test4_rateid(self):
        # test that emissionrate has an id assigned
        source = EmissionSource(name="U238", rate="10 +- 0.2 Bq/kg")
        self.assertEqual(source.rate.id, source.id)
