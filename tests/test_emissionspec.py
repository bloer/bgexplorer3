import unittest
from mongoengine import connect, disconnect, Document, ValidationError
from bgexplorer.models.emissionspec import EmissionSource, EmissionSpec, Multiplier
from bgexplorer.models.common import units, DimensionalityError
from bgexplorer.models.fields import get_fromstr
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

    def test2_getvalue(self):
        """ every multiplier reads its value from a Component """
        from bgexplorer.models.component import Component
        c = Component(name='c', mass='2 kg', volume='3 m**3',
                      inner_surface_area='4 m**2',
                      outer_surface_area='5 m**2', length='6 m')
        expected = {Multiplier.mass: '2 kg', Multiplier.volume: '3 m**3',
                    Multiplier.surface: '9 m**2',
                    Multiplier.inner_surface: '4 m**2',
                    Multiplier.outer_surface: '5 m**2',
                    Multiplier.length: '6 m'}
        for mult in Multiplier:
            with self.subTest(mult=mult):
                if mult is Multiplier.none:
                    self.assertEqual(mult.getvalue(c), 1)
                else:
                    self.assertEqual(mult.getvalue(c), units(expected[mult]))

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

    def names(self, spec):
        return sorted(s.name for s in spec.sources)

    def test3_autogen_user_source_wins(self):
        """ generated sources are defaults, used only if the user doesn't
        give the same source
        """
        u238 = EmissionSource(name='U238', rate='10 +- 1 mBq/kg')
        ra226 = EmissionSource(name='Ra226', rate='3 +- 1 mBq/kg')
        spec = EmissionSpec(name='', sources=[u238, ra226])
        spec.validate()
        self.assertEqual(self.names(spec), ['Ra226', 'U235', 'U238'])
        self.assertIsNone(spec.sourcemap['Ra226'].generated_from)
        self.assertEqual(get_fromstr(spec.sourcemap['Ra226'].rate), '3 +- 1 mBq/kg')
        self.assertEqual(spec.sourcemap['U235'].generated_from, u238.id)
        spec.validate()
        self.assertEqual(self.names(spec), ['Ra226', 'U235', 'U238'])

        # adding the user's source replaces an existing generated one
        spec = EmissionSpec(name='', sources=[u238])
        spec.validate()
        self.assertIsNotNone(spec.sourcemap['Ra226'].generated_from)
        spec.sources.append(EmissionSource(name='Ra-226', rate='2 mBq/kg'))
        spec.validate()
        self.assertEqual(self.names(spec), ['Ra-226', 'U235', 'U238'])

    def test3_autogen_follows_parent(self):
        spec = EmissionSpec(name='', sources=[
            EmissionSource(name='U238', rate='10 +- 1 mBq/kg')])
        spec.validate()
        spec.sources[0].rate = '20 +- 1 mBq/kg'
        spec.validate()
        self.assertAlmostEqual(spec.sourcemap['Ra226'].rate.to('mBq/kg').m
                               .mode, 20)
        self.assertEqual(self.names(spec), ['Ra226', 'U235', 'U238'])

    def test3_autogen_override(self):
        """ an overridden default keeps its value; removing it restores the
        default
        """
        spec = EmissionSpec(name='', sources=[
            EmissionSource(name='U238', rate='10 +- 1 mBq/kg')])
        spec.validate()
        ra226 = spec.sourcemap['Ra226']
        ra226.rate = '4 +- 1 mBq/kg'
        spec.mark_overrides({str(ra226.id)})
        self.assertIsNone(ra226.generated_from)
        spec.sources[0].rate = '20 +- 1 mBq/kg'
        spec.validate()
        self.assertEqual(get_fromstr(spec.sourcemap['Ra226'].rate), '4 +- 1 mBq/kg')
        self.assertEqual(self.names(spec), ['Ra226', 'U235', 'U238'])
        # sources that aren't generated, or weren't edited, are unchanged
        u235 = spec.sourcemap['U235']
        spec.mark_overrides({str(spec.sources[0].id), 'nope'})
        self.assertIsNotNone(u235.generated_from)

        spec.sources.remove(spec.sourcemap['Ra226'])
        spec.validate()
        self.assertAlmostEqual(spec.sourcemap['Ra226'].rate.to('mBq/kg').m
                               .mode, 20)
        self.assertIsNotNone(spec.sourcemap['Ra226'].generated_from)

    def test2_spec_multiplier(self):
        """ the spec's multiplier is used by every source it suits """
        spec = EmissionSpec(name='mixed', multiplier=Multiplier.outer_surface,
                            sources=[EmissionSource(name='Pb210',
                                                    rate='1 mBq/m**2'),
                                     EmissionSource(name='K40',
                                                    rate='1 mBq/kg')]).save()
        self.assertEqual([s.multiplier for s in spec.sources],
                         [Multiplier.outer_surface, Multiplier.mass])
        # sources store their multiplier, but changing ours still applies
        spec.multiplier = Multiplier.inner_surface
        spec.save()
        self.assertEqual(spec.reload().sources[0].multiplier,
                         Multiplier.inner_surface)
        # without one, sources keep their own
        spec.multiplier = None
        spec.sources[0].multiplier = Multiplier.surface
        spec.save()
        self.assertEqual(spec.reload().sources[0].multiplier,
                         Multiplier.surface)

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
