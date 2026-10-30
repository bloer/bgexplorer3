import unittest
from unittest import mock
from mongoengine import (connect, disconnect, StringField, ReferenceField,
                         ValidationError,
                         CASCADE, NULLIFY, PULL)
from bgexplorer.models.component import (Component, Placement, Assembly,
                                         ComponentSpec, LocationMode)
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.assay import Assay
from bgexplorer.models.sourceterm import (SourceTerm, CalculatedResults,
                                          ResultsCache, clear_results_cache)
from bgexplorer.models.settings import VersionSettings, get_settings
from bgexplorer.models import signals
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.common import units
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.isotope import concentration_to_rate
import numpy as np
from tests.dbutil import connect_test_db


def spec_of(*sources) -> EmissionSpec:
    """ A saved spec with just `sources` """
    return EmissionSpec(name=f"{sources[0].name} spec", sources=list(sources)
                        ).save()

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
                       specs=[e1, spec_of(EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg"))]).save()
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
                       specs=[spec_of(EmissionSource(name="U238", rate="81 ppb"),
                                      EmissionSource(name="K40", rate="32.3 ppm"))],
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
                       specs=[e1, spec_of(EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg"))]).save()
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
                       specs=[e1, spec_of(EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg"))]).save()
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
        c1.specs = [e1]
        c1.save()
        expected.update({'a1/c1': 2, 'a2/a1/c1': 2})
        self.assertEqual(counts(), expected)

    def test2_confidence_rating(self):
        """ SourceTerms copy their leaf component's rating, and follow it """
        e1 = EmissionSpec(name="e1", sources=[
            EmissionSource(name="Th232", rate="10 mBq/kg")]).save()
        c1 = Component(name="c1", mass="2 kg", specs=[e1],
                       confidence_rating=4).save()
        c2 = Component(name="c2", mass="2 kg", specs=[e1]).save()
        a1 = Assembly(name="a1", components=[c1, c2]).save()

        def ratings():
            return {st.assemblyPathStr: st.confidence_rating
                    for st in SourceTerm.objects}
        self.assertEqual(ratings(), {'c1': 4, 'c2': None, 'a1/c1': 4,
                                     'a1/c2': None})

        c1.reload()
        c1.confidence_rating = 2
        c1.save()
        c2.reload()
        c2.confidence_rating = 5
        c2.save()
        self.assertEqual(ratings(), {'c1': 2, 'c2': 5, 'a1/c1': 2,
                                     'a1/c2': 5})
        c1.confidence_rating = None
        c1.save()
        self.assertEqual(ratings()['a1/c1'], None)

        # a rating on the attached spec beats the component's
        e2 = EmissionSpec(name="e2", sources=[
            EmissionSource(name="K40", rate="10 mBq/kg")]).save()
        c1.attach(e2, confidence_rating=1)
        c1.confidence_rating = 3
        c1.save()
        ratings = {st.source.name: st.confidence_rating
                   for st in SourceTerm.objects(assemblyPathStr='a1/c1')}
        self.assertEqual(ratings, {'Th232': 3, 'K40': 1})

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
                       specs=[e1, spec_of(EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg"))]).save()
        c2 = Component(name="c2", mass="2 kg",
                       specs=[e1, spec_of(EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg"))]).save()
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

        # nothing is stored unless asked
        CalculatedResults.for_object(a1, cache=False)
        self.assertEqual(CalculatedResults.objects.count(), 0)
        CalculatedResults.for_object(a1, save=True, cache=False)
        self.assertEqual(CalculatedResults.objects.count(), 1)
        CalculatedResults.drop_collection()

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
                       specs=[spec_of(EmissionSource(name='U238', rate='5 mBq/kg'))]).save()
        c2 = Component(name='c2', mass='8 kg', location='c1',
                       specs=[spec_of(EmissionSource(name='U238', rate='5 microBq/kg'))]).save()
        r1 = CalculatedResults.for_component(c1)
        r2 = CalculatedResults.for_component(c2)

        # the settings may add other sources, which have no hiteffs here
        def u238(result):
            return [st for st in result.sources if st.source.name == 'U238']
        self.assertEqual(len(u238(r1)), 1)
        self.assertEqual(len(u238(r1)[0].hiteffs), 3)
        self.assertAlmostEqual(r1.scalars['v1'].to('dru').mode, 15*(10 + 2e-2 + 2e-1))
        self.assertEqual(len(u238(r2)), 1)
        self.assertEqual(len(u238(r2)[0].hiteffs), 2)
        self.assertAlmostEqual(r2.scalars['v1'].to('dru').mode, 0.04*(10 + 2e-2))






    def test5_cache(self):
        """ Results are cached in memory until data in the version change """
        VersionSettings.drop_collection()
        get_settings()
        clear_results_cache()
        h = HitEfficiency(source="Co60", location="loc", scalars=dict(
            v1=AsymmetricUncertainty(0.1, 0.01)*units('dru/mBq'))).save()
        c = Component(name="c", mass="2 kg", location="loc", specs=[spec_of(
            EmissionSource(name="Co60", rate="10 +- 1 mBq/kg"))]).save()
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
        c.mass = "4 kg"
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

    def test6_stamps(self):
        """ A SourceTerm's stamp only changes when its content does """
        VersionSettings.drop_collection()
        settings = get_settings()
        HitEfficiency(source="Co60", location="loc", scalars=dict(
            v1=AsymmetricUncertainty(0.1, 0.01)*units('dru/mBq'))).save()
        e1 = spec_of(EmissionSource(name="Co60", rate="10 +- 1 mBq/kg"))
        c = Component(name="c", mass="2 kg", location="loc",
                      specs=[e1]).save()
        a = Assembly(name="a", components=[c]).save()

        def stamps():
            return {st.assemblyPathStr: st.stamp for st in SourceTerm.objects}
        before = stamps()
        self.assertEqual(len(before), 2)
        self.assertEqual(len(set(before.values())), 2)
        self.assertTrue(all(before.values()))
        # saved and recalculated values agree
        for st in SourceTerm.objects:
            self.assertEqual(st.stamp, st.content_stamp())

        def tokens():
            settings.reload()
            return settings.cache_token, settings.calc_token
        tokens_before = tokens()
        # saving again changes nothing about the terms, nor the stamps
        c.reload()
        c.save()
        self.assertEqual(stamps(), before)
        # edits to components change the cache token but not the
        # calculation token, since the stamps cover them
        self.assertNotEqual(tokens()[0], tokens_before[0])
        self.assertEqual(tokens()[1], tokens_before[1])

        c.confidence_rating = 3
        c.save()
        after = stamps()
        self.assertNotEqual(after['c'], before['c'])
        self.assertNotEqual(after['a/c'], before['a/c'])
        self.assertEqual(
            {st.assemblyPathStr: st.stamp == st.content_stamp()
             for st in SourceTerm.objects}, {'c': True, 'a/c': True})

        a.reload()
        a.children[0].weight = 2
        a.save()
        weighted = stamps()
        self.assertEqual(weighted['c'], after['c'])
        self.assertNotEqual(weighted['a/c'], after['a/c'])

        # HitEfficiencies change the calculation token, and the stamps of
        # the SourceTerms using them
        h = HitEfficiency.objects.get(source="Co60")
        calc = tokens()[1]
        h.scalars = dict(v1=AsymmetricUncertainty(0.2, 0.01)*units('dru/mBq'))
        h.save()
        self.assertNotEqual(tokens()[1], calc)
        self.assertEqual(stamps(), weighted)
        h.delete()
        self.assertNotEqual(stamps()['c'], weighted['c'])

    def test7_sync(self):
        """ Updates only write the SourceTerms that changed, keep the ids of
        those that stay, and only go up the tree while something changes
        """
        e1 = spec_of(EmissionSource(name="Co60", rate="10 +- 1 mBq/kg"),
                     EmissionSource(name="K40", rate="5 +- 1 mBq/kg"))
        c = Component(name="c", mass="2 kg", specs=[e1]).save()
        c2 = Component(name="c2", mass="2 kg", specs=[e1]).save()
        a1 = Assembly(name="a1", components=[c, c2]).save()
        a2 = Assembly(name="a2", components=[a1]).save()

        def state():
            return {(st.assemblyPathStr, st.source.name):
                    (st.id, st.revision, st.stamp)
                    for st in SourceTerm.objects}
        before = state()
        self.assertEqual(len(before), 2 + 2 + 4 + 4)

        # nothing changed: nothing is written, and only the direct parent
        # is looked at
        with mock.patch.object(signals, '_sync_placements',
                               wraps=signals._sync_placements) as sync:
            c.save()
        self.assertEqual(state(), before)
        self.assertEqual(sync.call_count, 1)
        a1.save()
        a2.save()
        self.assertEqual(state(), before)

        # a change goes up the whole tree, and only to the terms it affects
        c.reload()
        c.confidence_rating = 3
        with mock.patch.object(signals, '_sync_placements',
                               wraps=signals._sync_placements) as sync:
            c.save()
        self.assertEqual(sync.call_count, 2)
        after = state()
        self.assertEqual(set(after), set(before))
        for key, (_id, revision, stamp) in after.items():
            self.assertEqual(_id, before[key][0])
            if key[0].endswith('/c') or key[0] == 'c':
                self.assertEqual(revision, before[key][1] + 1, key)
                self.assertNotEqual(stamp, before[key][2], key)
            else:
                self.assertEqual((revision, stamp), before[key][1:], key)

        # SourceTerms that are no longer wanted are removed
        a1.reload()
        a1.children = [p for p in a1.children if p.component.name == 'c2']
        a1.save()
        self.assertEqual({path for path, _ in state()},
                         {'c', 'c2', 'a1/c2', 'a2/a1/c2'})
        self.assertEqual(SourceTerm.objects.count(), 8)


class TestLocationOverride(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in (Component, Assembly, EmissionSpec, SourceTerm,
                    CalculatedResults, HitEfficiency):
            cls.drop_collection()
        eff = AsymmetricUncertainty(0.01, 0.001) * units('dru/mBq')
        for source in ('U238', 'Pb210'):
            for location in ('Package', 'Package Outer Surface',
                             'Packge Inner Surface', 'Connector Inside',
                             'Connector Outside'):
                HitEfficiency(source=source, location=location,
                              scalars=dict(v1=eff)).save()
        self.becu = EmissionSpec(name="BeCu", sources=dict(
            U238="1 mBq/kg", Pb210="1 mBq/kg")).save()
        self.radon = EmissionSpec(name="radon", sources=dict(
            Pb210="1 mBq/kg")).save()

    def locations(self, **query):
        """ {(assemblyPathStr, spec name, source name): location}, ignoring
        sources generated by the settings' addsources
        """
        return {(st.assemblyPathStr, st.spec.name if st.spec else None,
                 st.source.name): st.location
                for st in SourceTerm.objects(source__name__in=['U238', 'Pb210'],
                                             **query)}

    def hiteff_locations(self, path, spec, source):
        st = [st for st in SourceTerm.objects(assemblyPathStr=path,
                                              source__name=source)
              if (st.spec and st.spec.name) == spec][0]
        return [h.location for h in st.hiteffs]

    def test_replace(self):
        inner = Component(name="inner", mass="1 kg",
                          location="Connector Inside",
                          specs=[self.becu]).save()
        outer = Component(name="outer", mass="1 kg",
                          location="Connector Outside",
                          specs=[self.becu]).save()
        package = Component(name="package", mass="1 kg", location="Package")
        package.attach(self.becu)
        package.attach(self.radon, location="Package Outer Surface")
        package.save()
        inner.attach(self.radon, location="Packge Inner Surface")
        inner.save()
        self.assertEqual(self.locations(), {
            ('inner', 'BeCu', 'U238'): 'Connector Inside',
            ('inner', 'BeCu', 'Pb210'): 'Connector Inside',
            ('inner', 'radon', 'Pb210'): 'Packge Inner Surface',
            ('outer', 'BeCu', 'U238'): 'Connector Outside',
            ('outer', 'BeCu', 'Pb210'): 'Connector Outside',
            ('package', 'BeCu', 'U238'): 'Package',
            ('package', 'BeCu', 'Pb210'): 'Package',
            ('package', 'radon', 'Pb210'): 'Package Outer Surface',
        })
        self.assertEqual(self.hiteff_locations('inner', 'radon', 'Pb210'),
                         ['Packge Inner Surface'])
        st = SourceTerm.objects(assemblyPathStr='package',
                                spec=self.radon).get()
        self.assertEqual(st.location_origin, "override on package")
        self.assertTrue(st.location_overridden)
        self.assertFalse(SourceTerm.objects(assemblyPathStr='package',
                                            spec=self.becu,
                                            source__name='U238')
                         .get().location_overridden)

        # placing them keeps their own settings
        a1 = Assembly(name="a1", location="Package", components=[
            inner, outer, package]).save()
        self.assertEqual(self.locations(assemblyRoot=a1)[
            ('a1/inner', 'radon', 'Pb210')], 'Packge Inner Surface')

        # a new hiteff at the override location gets attached
        HitEfficiency(source='Pb210', location='Packge Inner Surface',
                      scalars=dict(v2=AsymmetricUncertainty(0.01, 0.001)
                                   * units('dru/mBq'))).save()
        self.assertEqual(len(self.hiteff_locations('a1/inner', 'radon',
                                                   'Pb210')), 2)

        # clearing the location reverts matching
        inner.reload()
        inner.find_spec_entry(self.radon).location = None
        inner.save()
        self.assertEqual(self.hiteff_locations('a1/inner', 'radon', 'Pb210'),
                         ['Connector Inside'])

        # settings are config, so survive rebuilding the sourceterms
        SourceTerm.drop_collection()
        for component in (inner, outer, package):
            component.reload().save()
        self.assertEqual(self.locations(assemblyRoot=package)[
            ('package', 'radon', 'Pb210')], 'Package Outer Surface')

    def test_prefix_suffix(self):
        screws = Component(name="screws", mass="1 kg")
        screws.attach(self.becu)
        screws.attach(self.radon, location=" Outer Surface",
                      location_mode=LocationMode.prefix)
        screws.save()
        a1 = Assembly(name="a1", children=[
            Placement(component=screws, label="in", location="Package"),
            Placement(component=screws, label="out", location="Connector"),
        ]).save()
        locations = self.locations(assemblyRoot=a1)
        self.assertEqual(locations[('a1/in', 'radon', 'Pb210')],
                         " Outer SurfacePackage")
        self.assertEqual(locations[('a1/out', 'radon', 'Pb210')],
                         " Outer SurfaceConnector")
        self.assertEqual(locations[('a1/out', 'BeCu', 'Pb210')], 'Connector')

        screws.reload()
        entry = screws.find_spec_entry(self.radon)
        entry.location = " Outer Surface"
        entry.location_mode = LocationMode.suffix
        screws.save()
        locations = self.locations(assemblyRoot=a1)
        self.assertEqual(locations[('a1/in', 'radon', 'Pb210')],
                         "Package Outer Surface")
        st = SourceTerm.objects(assemblyPathStr='a1/out',
                                spec=self.radon).get()
        self.assertEqual(st.location, "Connector Outer Surface")
        self.assertEqual(st.location_origin,
                         "override on screws (suffix to placement a1/out)")
        self.assertTrue(st.location_overridden)

        # with no location set anywhere, the component's name is the base
        bare = Component(name="bare", mass="1 kg")
        bare.attach(self.radon, location=" surface",
                    location_mode=LocationMode.prefix)
        bare.save()
        self.assertEqual(self.locations(assemblyRoot=bare),
                         {('bare', 'radon', 'Pb210'): ' surfacebare'})

    def test_specs_and_cleanup(self):
        c1 = Component(name="c1", mass="1 kg", location="Package",
                       specs=[self.becu, self.radon])
        self.assertEqual([e.spec.name for e in c1.spec_entries],
                         ['BeCu', 'radon'])
        with self.assertRaises(ValueError):
            Component(name="c", specs=[self.becu],
                      spec_entries=[ComponentSpec(spec=self.radon)])
        c1.find_spec_entry(self.radon).location = "Package Outer Surface"
        c1.save()
        # replacing the specs keeps the settings of those that remain
        c1.specs = [self.radon]
        self.assertEqual(c1.find_spec_entry(self.radon).location,
                         "Package Outer Surface")
        self.assertIsNone(c1.find_spec_entry(self.becu))
        c1.save()
        self.assertEqual(self.locations(), {
            ('c1', 'radon', 'Pb210'): 'Package Outer Surface'})
        # a spec can only be attached once
        c1.attach(self.radon)
        with self.assertRaises(ValidationError):
            c1.save()

    def test_delete_spec(self):
        c1 = Component(name="c1", mass="1 kg", specs=[self.becu, self.radon],
                       confidence_rating=2).save()
        self.radon.delete()
        c1 = Component.objects.get(name="c1")
        self.assertEqual([s.name for s in c1.specs], ['BeCu'])
        self.assertEqual(self.locations(), {
            ('c1', 'BeCu', 'U238'): 'c1', ('c1', 'BeCu', 'Pb210'): 'c1'})


class TestResultsCache(unittest.TestCase):
    def test_cached(self):
        cache = ResultsCache(2)
        calls = []
        for key in ('a', 'a', 'b', 'c', 'a'):
            cache.get((key,), lambda: calls.append(key) or key)
        # 'a' was evicted by 'c', and None isn't cached
        self.assertEqual(calls, ['a', 'b', 'c', 'a'])
        cache.get(None, lambda: calls.append('none'))
        cache.get(None, lambda: calls.append('none'))
        self.assertEqual(calls[-2:], ['none', 'none'])

    def test_single_flight(self):
        """ concurrent requests for a key share one calculation """
        from concurrent.futures import ThreadPoolExecutor
        import threading
        cache = ResultsCache(4)
        calls = []
        started = threading.Event()
        release = threading.Event()

        def calculate():
            calls.append(1)
            started.set()
            release.wait(5)
            return 'value'

        with ThreadPoolExecutor(6) as pool:
            first = pool.submit(cache.get, ('k',), calculate)
            self.assertTrue(started.wait(5))
            others = [pool.submit(cache.get, ('k',), calculate)
                      for _ in range(4)]
            # a different key isn't held up
            self.assertEqual(cache.get(('other',), lambda: 'o'), 'o')
            release.set()
            results = [f.result(5) for f in [first] + others]
        self.assertEqual(results, ['value'] * 5)
        self.assertEqual(len(calls), 1)

    def test_failure_not_shared(self):
        """ a failed calculation isn't cached, and waiters try for themselves
        """
        from concurrent.futures import ThreadPoolExecutor
        import threading
        cache = ResultsCache(4)
        started = threading.Event()
        release = threading.Event()

        def fails():
            started.set()
            release.wait(5)
            raise RuntimeError("failed")

        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(cache.get, ('k',), fails)
            self.assertTrue(started.wait(5))
            second = pool.submit(cache.get, ('k',), lambda: 'ok')
            release.set()
            with self.assertRaises(RuntimeError):
                first.result(5)
            self.assertEqual(second.result(5), 'ok')
        self.assertEqual(cache.get(('k',), lambda: 'again'), 'ok')

    def test_reentrant(self):
        cache = ResultsCache(4)
        self.assertEqual(
            cache.get(('k',), lambda: cache.get(('k',), lambda: 'inner')),
            'inner')
