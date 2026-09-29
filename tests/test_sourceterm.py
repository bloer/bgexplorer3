import unittest
from unittest import mock
from mongoengine import (connect, disconnect, StringField, ReferenceField,
                         CASCADE, NULLIFY, PULL)
from bgexplorer.models.component import Component, Placement, Assembly
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.assay import Assay
from bgexplorer.models.sourceterm import (SourceTerm, CalculatedResults,
                                          clear_results_cache)
from bgexplorer.models.settings import VersionSettings, get_settings
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.common import units
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.isotope import concentration_to_rate
import numpy as np
from tests.dbutil import connect_test_db

class TestSourceTerm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        Component.drop_collection()
        Assembly.drop_collection()
        EmissionSpec.drop_collection()
        SourceTerm.drop_collection()
        CalculatedResults.drop_collection()
        HitEfficiency.drop_collection()

    def tearDown(self):
        pass

    def test1_savecomponent(self):
        e1 = EmissionSpec(name="e1", sources=[EmissionSource(name="Th232", rate="10 +- 1 mBq/kg"),
                                              EmissionSource(name="K40", rate="<25 mBq/kg")]).save()
        c1 = Component(name="c1", mass="2 kg",
                       sources=[EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg")],
                       specs=[e1]).save()
        self.assertEqual(SourceTerm.objects.count(), 3)
        emissionrates = dict(Th232=AsymmetricUncertainty(20, 2)*units('mBq/kg'),
                             K40=AsymmetricUncertainty.fromlimit(50)*units('mBq/kg'),
                             Co60=AsymmetricUncertainty(40, 0.4)*units('mBq/kg'))
        for st in SourceTerm.objects:
            self.assertAlmostEqual(st.emissionrate.mode, emissionrates[st.source.name].mode)
            self.assertAlmostEqual(st.emissionrate.s0, emissionrates[st.source.name].s0)

        e1.reload() # without this, we get a weakref deleted error...
        e1.sources[0].rate = AsymmetricUncertainty(8, 2)*units('mBq/kg')
        e1.save()
        self.assertEqual(SourceTerm.objects.count(), 3)
        emissionrates = dict(Th232=AsymmetricUncertainty(16, 4)*units('mBq/kg'),
                             K40=AsymmetricUncertainty.fromlimit(50)*units('mBq/kg'),
                             Co60=AsymmetricUncertainty(40, 0.4)*units('mBq/kg'))
        for st in SourceTerm.objects:
            self.assertAlmostEqual(st.emissionrate.mode, emissionrates[st.source.name].mode)
            self.assertAlmostEqual(st.emissionrate.s0, emissionrates[st.source.name].s0)

        c1.mass = 4*units.kg
        c1.save()
        self.assertEqual(SourceTerm.objects.count(), 3)
        emissionrates = dict(Th232=AsymmetricUncertainty(32, 8)*units('mBq/kg'),
                             K40=AsymmetricUncertainty.fromlimit(100)*units('mBq/kg'),
                             Co60=AsymmetricUncertainty(80, 0.8)*units('mBq/kg'))
        for st in SourceTerm.objects:
            self.assertAlmostEqual(st.emissionrate.mode, emissionrates[st.source.name].mode)
            self.assertAlmostEqual(st.emissionrate.s0, emissionrates[st.source.name].s0)

        e1.delete()
        self.assertEqual(SourceTerm.objects.count(), 1)


    def test1_concentration(self):
        """ Sources given as concentrations are converted to activities """
        c1 = Component(name="c1", mass="2 kg",
                       sources=[EmissionSource(name="U238", rate="81 ppb"),
                                EmissionSource(name="K40", rate="32.3 ppm")],
                       ).save()
        for name, conc in (('U238', 81*units.ppb), ('K40', 32.3*units.ppm)):
            st = SourceTerm.objects.get(source__name=name)
            expected = (2 * units.kg * concentration_to_rate(name, conc))
            self.assertAlmostEqual(st.emissionrate.to('Bq').mode,
                                   expected.to('Bq').m)

    def test1_spec_subclass(self):
        """ Saving a subclass of EmissionSpec updates its components """
        a1 = Assay(name="a1", sources=dict(Th232="10 mBq/kg")).save()
        Component(name="c1", mass="2 kg", specs=[a1]).save()
        st = SourceTerm.objects.get()
        self.assertAlmostEqual(st.emissionrate.to('mBq').mode, 20)

        a1.reload()
        a1.sources[0].rate = AsymmetricUncertainty(8, 1)*units('mBq/kg')
        a1.save()
        st = SourceTerm.objects.get()
        self.assertAlmostEqual(st.emissionrate.to('mBq').mode, 16)

    def test2_saveassembly(self):
        e1 = EmissionSpec(name="e1", sources=[EmissionSource(name="Th232", rate="10 +- 1 mBq/kg"),
                                              EmissionSource(name="K40", rate="<25 mBq/kg")]).save()
        c1 = Component(name="c1", mass="2 kg", location="c1 location",
                       sources=[EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg")],
                       specs=[e1]).save()
        c2 = Component(name="c2", mass="2 kg",
                       specs=[e1]).save()
        c3 = Component(name="c3", mass="2 kg",
                       specs=[e1]).save()
        a1 = Assembly(name="a1", components=[c1, c2, c3], location="a1 location").save()

        self.assertEqual(SourceTerm.objects.count(), 14)
        self.assertEqual(SourceTerm.objects(assemblyRoot=a1).count(), 7)

        st = SourceTerm.objects(assemblyPathStr="a1/c1").first()
        self.assertEqual(st.location, "c1 location")
        st = SourceTerm.objects(assemblyPathStr="a1/c2").first()
        self.assertEqual(st.location, "a1 location")
        st = SourceTerm.objects(assemblyPathStr="a1/c3").first()
        self.assertEqual(st.location, "a1 location")

        a1.reload()
        a1.children[1].location = "c2 placement location"
        a1.children[1].weight = 2
        a1.save()
        st = SourceTerm.objects(assemblyPathStr="a1/c2").first()
        self.assertEqual(st.location, "c2 placement location")
        self.assertEqual(st.weight, 2)

    def test2_nested_assembly(self):
        """ Saving a component must not delete sibling SourceTerms further
        up the assembly tree
        """
        e1 = EmissionSpec(name="e1", sources=[EmissionSource(name="Th232", rate="10 +- 1 mBq/kg"),
                                              EmissionSource(name="K40", rate="<25 mBq/kg")]).save()
        c1 = Component(name="c1", mass="2 kg",
                       sources=[EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg")],
                       specs=[e1]).save()
        c2 = Component(name="c2", mass="2 kg", specs=[e1]).save()
        a1 = Assembly(name="a1", components=[c1, c2]).save()
        a2 = Assembly(name="a2", components=[a1, c2]).save()

        def counts():
            return {path: SourceTerm.objects(assemblyPathStr=path).count()
                    for path in ('a1/c1', 'a1/c2', 'a2/a1/c1', 'a2/a1/c2',
                                 'a2/c2')}
        expected = {'a1/c1': 3, 'a1/c2': 2, 'a2/a1/c1': 3, 'a2/a1/c2': 2,
                    'a2/c2': 2}
        self.assertEqual(counts(), expected)
        c1.save()
        self.assertEqual(counts(), expected)
        c2.save()
        self.assertEqual(counts(), expected)

        # removing a source removes it everywhere, and only it
        c1.sources = []
        c1.save()
        expected.update({'a1/c1': 2, 'a2/a1/c1': 2})
        self.assertEqual(counts(), expected)

    def test3_hiteffs(self):
        """ Test that queries find HitEfficiencies """
        h1 = HitEfficiency(source="Th232", location="c1 location",
                           scalars=dict(v1=AsymmetricUncertainty(0.1, 0.02)*units('dru/mBq'))).save()
        h2 = HitEfficiency(source="Th232", location="a1 location",
                           scalars=dict(v1=AsymmetricUncertainty(0.5, 0.05)*units('dru/mBq'))).save()
        h3 = HitEfficiency(source="Co60", location="c2 placement location",
                           scalars=dict(v1=AsymmetricUncertainty(0.3, 0.02)*units('dru/mBq'))).save()
        h4 = HitEfficiency(source="custom", location='',
                           scalars=dict(v1=AsymmetricUncertainty(0.01, 0.002)*units('dru/mBq'))).save()
        e1 = EmissionSpec(name="e1", sources=[EmissionSource(name="Th232", rate="10 +- 1 mBq/kg"),
                                              EmissionSource(name="K40", rate="<25 mBq/kg")]).save()
        c1 = Component(name="c1", mass="2 kg", location="c1 location",
                       sources=[EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg")],
                       specs=[e1]).save()
        c2 = Component(name="c2", mass="2 kg",
                       sources=[EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg")],
                       specs=[e1]).save()
        c3 = Component(name="c3", mass="5 kg",
                       specs=[e1]).save()
        a1 = Assembly(name="a1", components=[c1, c2, c3], location="a1 location")
        a1.children[1].location = "c2 placement location"
        a1.children[1].weight = 2
        a1.children[2].weight = 3
        a1.save()

        for st in SourceTerm.objects:
            # if not in assembly, only c1 has a valid location, and only
            # Th232 is a match
            if st.assemblyRoot.id == c1.id:
                if st.source.name == "Th232":
                    self.assertEqual(len(st.hiteffs), 1)
                    self.assertEqual(st.hiteffs[0].id, h1.id)
                elif st.source.name == "K40":
                    self.assertEqual(len(st.hiteffs), 0)
                elif st.source.name == "Co60":
                    self.assertEqual(len(st.hiteffs), 0)
            elif st.assemblyRoot.id == c2.id:
                if st.source.name == "Th232":
                    self.assertEqual(len(st.hiteffs), 0)
                elif st.source.name == "K40":
                    self.assertEqual(len(st.hiteffs), 0)
                elif st.source.name == "Co60":
                    self.assertEqual(len(st.hiteffs), 0)
            elif st.assemblyRoot.id == c3.id:
                if st.source.name == "Th232":
                    self.assertEqual(len(st.hiteffs), 0)
                elif st.source.name == "K40":
                    self.assertEqual(len(st.hiteffs), 0)
                elif st.source.name == "Co60":
                    self.assertEqual(len(st.hiteffs), 0)
            # in assembly, each component has a different location
            # c1 is unchanged
            elif st.assemblyRoot.id == a1.id and st.component.id == c1.id:
                if st.source.name == "Th232":
                    self.assertEqual(len(st.hiteffs), 1)
                    self.assertEqual(st.hiteffs[0].id, h1.id)
                elif st.source.name == "K40":
                    self.assertEqual(len(st.hiteffs), 0)
                elif st.source.name == "Co60":
                    self.assertEqual(len(st.hiteffs), 0)
            # c2.location = "c2 placement location"
            elif st.assemblyRoot.id == a1.id and st.component.id == c2.id:
                if st.source.name == "Th232":
                    self.assertEqual(len(st.hiteffs), 0)
                elif st.source.name == "K40":
                    self.assertEqual(len(st.hiteffs), 0)
                elif st.source.name == "Co60":
                    self.assertEqual(len(st.hiteffs), 1)
                    self.assertEqual(st.hiteffs[0].id, h3.id)
            # c3 location is "a1 location"
            elif st.assemblyRoot.id == a1.id and st.component.id == c1.id:
                if st.source.name == "Th232":
                    self.assertEqual(len(st.hiteffs), 1)
                    self.assertEqual(st.hiteffs[0].id, h2.id)
                elif st.source.name == "K40":
                    self.assertEqual(len(st.hiteffs), 0)
                elif st.source.name == "Co60":
                    self.assertEqual(len(st.hiteffs), 0)

        # now set a manual hiteff to one, save a bunch and make sure they
        # don't change
        SourceTerm.objects(assemblyPathStr="c2", source__name="K40")\
                  .update(set__hiteffs_auto=False, set__hiteffs=[h4])
        c2.save()
        # saving c2 must regenerate all of its terms in a1, not just one
        self.assertEqual(SourceTerm.objects(assemblyPathStr="a1/c2").count(), 3)
        for st in SourceTerm.objects(source__name="K40"):
            if st.component.id == c2.id and st.source.name == "K40":
                self.assertFalse(st.hiteffs_auto)
                self.assertEqual(len(st.hiteffs), 1)
                self.assertEqual(st.hiteffs[0].id, h4.id)

        # now add a new hiteff that would match our custom one
        h5 = HitEfficiency(source="K40", location="c2 placement location",
                           scalars=dict(v1=AsymmetricUncertainty(0.02, 0.002)*units('dru/mBq'))).save()
        h6 = HitEfficiency(source="K40", location="a1 location",
                           scalars=dict(v1=AsymmetricUncertainty(0.03, 0.002)*units('dru/mBq'))).save()
        for st in SourceTerm.objects(source__name="K40"):
            if st.assemblyPathStr == "a1/c1":
                self.assertEqual(len(st.hiteffs), 0)
            elif st.assemblyPathStr == "a1/c2":
                self.assertFalse(st.hiteffs_auto)
                self.assertEqual(len(st.hiteffs), 1)
                self.assertEqual(st.hiteffs[0].id, h4.id)
            elif st.assemblyPathStr == "a1/c3":
                self.assertEqual(len(st.hiteffs), 1)
                self.assertEqual(st.hiteffs[0].id, h6.id)
            elif st.assemblyPathStr == "c1":
                self.assertEqual(len(st.hiteffs), 0)
            elif st.assemblyPathStr == "c2":
                self.assertFalse(st.hiteffs_auto)
                self.assertEqual(len(st.hiteffs), 1)
                self.assertEqual(st.hiteffs[0].id, h4.id)
            elif st.assemblyPathStr == "c3":
                self.assertEqual(len(st.hiteffs), 0)

        # check the results of individual calculations
        for st in SourceTerm.objects(hiteffs__size=1):
            cr = CalculatedResults.from_sourceterm(st).save()
            val = cr.scalars['v1'].to('dru')
            if st.assemblyPathStr == 'c1' and st.source.name == 'Th232':
                self.assertAlmostEqual(val.mode, 2)
                self.assertAlmostEqual(val.s0, 0.44899888641287294)
            elif st.assemblyPathStr == 'c2' and st.source.name == 'Co60':
                self.assertAlmostEqual(val.mode, 12)
                self.assertAlmostEqual(val.s0, 0.8089894931332544)
            elif st.assemblyPathStr == 'c2' and st.source.name == 'K40':
                self.assertEqual(val.mode, 0)
                self.assertEqual(val.s0, 0)
                self.assertAlmostEqual(val.s1, 0.30999837493401583)
            elif st.assemblyPathStr == 'a1/c1' and st.source.name == 'Th232':
                self.assertAlmostEqual(val.mode, 2)
                self.assertAlmostEqual(val.s0, 0.44899888641287294)
            elif st.assemblyPathStr == 'a1/c2' and st.source.name == 'Co60':
                self.assertAlmostEqual(val.mode, 24)
                self.assertAlmostEqual(val.s0, 1.6179789862665088)
            elif st.assemblyPathStr == 'a1/c2' and st.source.name == 'K40':
                self.assertEqual(val.mode, 0)
                self.assertEqual(val.s0, 0)
                self.assertAlmostEqual(val.s1, 0.6199967498680317)
            elif st.assemblyPathStr == 'a1/c3' and st.source.name == 'Th232':
                self.assertAlmostEqual(val.mode, 75)
                self.assertAlmostEqual(val.s0, 10.63308515906837)
            elif st.assemblyPathStr == 'a1/c3' and st.source.name == 'K40':
                self.assertEqual(val.mode, 0)
                self.assertEqual(val.s0, 0)
                self.assertAlmostEqual(val.s1, 6.854696429539697)


        # calculate the summed result for the assembly
        """ result =
        # th232
        10 +- 1 mBq/kg * (2 kg * 0.1 +- .02 dru/Bq + ) +
                          3 * 5 kg * 0.5 +-0.05 dru/Bq) +
        # K40
        < 25 mBq/kg * (2 * 2kg * 0.01 +- 0.002 dru/Bq +
                       3 * 5kg * 0.03 +- 0.002 dru/Bq) +
        # Co60
        20 +- 0.2 mBq/kg * (2 * 2kg * 0.3 +-0.02 dru/Bq)
        """
        cr = CalculatedResults.for_component(a1).save()
        val = cr.scalars['v1'].to('dru')
        self.assertAlmostEqual(val.mode, 101)
        self.assertAlmostEqual(val.s0, 10.903300234332724)
        self.assertAlmostEqual(val.s1, 13.212474184422296)

        # for_tree calculates every row at once, same as one at a time
        tree = CalculatedResults.for_tree(a1)
        self.assertEqual(set(tree), {a1.original_id, c1.original_id,
                                     c2.original_id, c3.original_id})
        for obj in (a1, c1, c2, c3):
            expected = CalculatedResults.for_object(obj, relativeto=a1,
                                                    save=False)
            got = tree[obj.original_id].scalars['v1'].to('dru')
            expected = expected.scalars['v1'].to('dru')
            self.assertAlmostEqual(got.mode, expected.mode)
            self.assertAlmostEqual(got.s0, expected.s0)
            self.assertAlmostEqual(got.s1, expected.s1)
        # a separately loaded root is still the root
        root = Assembly.objects.get(id=a1.id)
        cr = CalculatedResults.for_object(root, relativeto=a1, save=False)
        self.assertAlmostEqual(cr.scalars['v1'].to('dru').mode, 101)

        # scalar-only results are never cached
        CalculatedResults.drop_collection()
        CalculatedResults.for_object(a1, save=True, spectra=False)
        self.assertEqual(CalculatedResults.objects.count(), 0)

        # test that hiteffs are removed appropriately
        h1.location = 'somewhere else'
        h1.save()
        st = SourceTerm.objects(assemblyRoot=c1, source__name='Th232').first()
        self.assertEqual(len(st.hiteffs), 0)

    def test4_neutron(self):
        """ test that we properly find multiple hits for neutrons """
        h1 = HitEfficiency(source='U238', location='c1', scalars=dict(v1='10 +- 1 dru/mBq')).save()
        h2 = HitEfficiency(source='U238', location='c1', primary_particle='neutron', primary_yield=1.e-2,
                           scalars=dict(v1='(2 +- 0.1)e-2 dru/mBq')).save()
        h3 = HitEfficiency(source='U238', location='c1', primary_particle='neutron', primary_yield=1.e-1, material='steel',
                           scalars=dict(v1='(2 +- 0.1)e-1 dru/mBq')).save()
        c1 = Component(name='c1', mass='3 kg', location='c1', material='steel',
                       sources=[EmissionSource(name='U238', rate='5 mBq/kg')]).save()
        c2 = Component(name='c2', mass='8 kg', location='c1',
                       sources=[EmissionSource(name='U238', rate='5 microBq/kg')]).save()
        r1 = CalculatedResults.for_component(c1)
        r2 = CalculatedResults.for_component(c2)

        self.assertEqual(len(r1.sources), 1)
        self.assertEqual(len(r1.sources[0].hiteffs), 3)
        self.assertAlmostEqual(r1.scalars['v1'].to('dru').mode, 15*(10 + 2e-2 + 2e-1))
        self.assertEqual(len(r2.sources), 1)
        self.assertEqual(len(r2.sources[0].hiteffs), 2)
        self.assertAlmostEqual(r2.scalars['v1'].to('dru').mode, 0.04*(10 + 2e-2))






    def test5_cache(self):
        """ Results are cached in memory until data in the version change """
        VersionSettings.drop_collection()
        get_settings()
        clear_results_cache()
        h = HitEfficiency(source="Co60", location="loc", scalars=dict(
            v1=AsymmetricUncertainty(0.1, 0.01)*units('dru/mBq'))).save()
        c = Component(name="c", mass="2 kg", location="loc", sources=[
            EmissionSource(name="Co60", rate="10 +- 1 mBq/kg")]).save()
        a = Assembly(name="a", components=[c]).save()

        def results(**kwargs):
            obj = CalculatedResults.for_object(c, **kwargs)
            tree = CalculatedResults.for_tree(a, **kwargs)[c.original_id]
            return [r.scalars['v1'].to('dru').mode for r in (obj, tree)]

        assert_allclose = np.testing.assert_allclose
        assert_allclose(results(), [2, 2])
        # repeated calls don't calculate
        with mock.patch.object(CalculatedResults, '_calculate',
                               side_effect=AssertionError("calculated")):
            assert_allclose(results(), [2, 2])
            with self.assertRaises(AssertionError):
                results(cache=False)

        # editing a component changes the result
        c.sources[0].rate = "20 +- 1 mBq/kg"
        c.save()
        assert_allclose(results(), [4, 4])
        # so does changing hiteff values, which doesn't touch sourceterms
        h.scalars['v1'] = AsymmetricUncertainty(0.2, 0.01)*units('dru/mBq')
        h.save()
        # for_object would find the old result in the database if it
        # weren't cleared
        assert_allclose(results(), [8, 8])
        assert_allclose(results(cache=False), [8, 8])

        # cloned settings get a new token
        settings = get_settings()
        clone = settings.clone('other')
        self.assertNotEqual(settings.cache_token, clone.cache_token)
