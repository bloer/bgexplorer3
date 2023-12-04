import unittest
from mongoengine import (connect, disconnect, StringField, ReferenceField,
                         CASCADE, NULLIFY, PULL)
from bgexplorer.models.component import Component, Placement, Assembly
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.sourceterm import SourceTerm, CalculatedResults
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.common import units
from bgexplorer.models.asymmetric import AsymmetricError
import numpy as np

class TestSourceTerm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # TODO: try to use mongo, and if it fails, switch to monomock
        # and add an expected failure for all $merge pipelines
        connect(uuidRepresentation='standard')
        #connect('mongoenginetest', host='mongodb://localhost',
        #        mongo_client_class=mongomock.MongoClient,
        #        uuidRepresentation='stanard')

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        Component.drop_collection()
        Assembly.drop_collection()
        Placement.drop_collection()
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
                       reference_specs=[e1]).save()
        self.assertEqual(SourceTerm.objects.count(), 3)
        emissionrates = dict(Th232=AsymmetricError(20, 2)*units('mBq/kg'),
                             K40=AsymmetricError.fromlimit(50)*units('mBq/kg'),
                             Co60=AsymmetricError(40, 0.4)*units('mBq/kg'))
        for st in SourceTerm.objects:
            self.assertAlmostEqual(st.emissionrate.mode, emissionrates[st.source.name].mode)
            self.assertAlmostEqual(st.emissionrate.s0, emissionrates[st.source.name].s0)

        e1.reload() # without this, we get a weakref deleted error...
        e1.sources[0].rate = AsymmetricError(8, 2)*units('mBq/kg')
        e1.save()
        self.assertEqual(SourceTerm.objects.count(), 3)
        emissionrates = dict(Th232=AsymmetricError(16, 4)*units('mBq/kg'),
                             K40=AsymmetricError.fromlimit(50)*units('mBq/kg'),
                             Co60=AsymmetricError(40, 0.4)*units('mBq/kg'))
        for st in SourceTerm.objects:
            self.assertAlmostEqual(st.emissionrate.mode, emissionrates[st.source.name].mode)
            self.assertAlmostEqual(st.emissionrate.s0, emissionrates[st.source.name].s0)

        c1.mass = 4*units.kg
        c1.save()
        self.assertEqual(SourceTerm.objects.count(), 3)
        emissionrates = dict(Th232=AsymmetricError(32, 8)*units('mBq/kg'),
                             K40=AsymmetricError.fromlimit(100)*units('mBq/kg'),
                             Co60=AsymmetricError(80, 0.8)*units('mBq/kg'))
        for st in SourceTerm.objects:
            self.assertAlmostEqual(st.emissionrate.mode, emissionrates[st.source.name].mode)
            self.assertAlmostEqual(st.emissionrate.s0, emissionrates[st.source.name].s0)

        e1.delete()
        self.assertEqual(SourceTerm.objects.count(), 1)


    def test2_saveassembly(self):
        e1 = EmissionSpec(name="e1", sources=[EmissionSource(name="Th232", rate="10 +- 1 mBq/kg"),
                                              EmissionSource(name="K40", rate="<25 mBq/kg")]).save()
        c1 = Component(name="c1", mass="2 kg", location="c1 location",
                       sources=[EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg")],
                       reference_specs=[e1]).save()
        c2 = Component(name="c2", mass="2 kg",
                       reference_specs=[e1]).save()
        c3 = Component(name="c3", mass="2 kg",
                       reference_specs=[e1]).save()
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

    def test3_hiteffs(self):
        """ Test that queries find HitEfficiencies """
        h1 = HitEfficiency(source="Th232", location="c1 location",
                           values=dict(v1=AsymmetricError(0.1, 0.02)*units('dru/mBq'))).save()
        h2 = HitEfficiency(source="Th232", location="a1 location",
                           values=dict(v1=AsymmetricError(0.5, 0.05)*units('dru/mBq'))).save()
        h3 = HitEfficiency(source="Co60", location="c2 placement location",
                           values=dict(v1=AsymmetricError(0.3, 0.02)*units('dru/mBq'))).save()
        h4 = HitEfficiency(source="custom", location='',
                           values=dict(v1=AsymmetricError(0.01, 0.002)*units('dru/mBq'))).save()
        e1 = EmissionSpec(name="e1", sources=[EmissionSource(name="Th232", rate="10 +- 1 mBq/kg"),
                                              EmissionSource(name="K40", rate="<25 mBq/kg")]).save()
        c1 = Component(name="c1", mass="2 kg", location="c1 location",
                       sources=[EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg")],
                       reference_specs=[e1]).save()
        c2 = Component(name="c2", mass="2 kg",
                       sources=[EmissionSource(name="Co60", rate="20 +- 0.2 mBq/kg")],
                       reference_specs=[e1]).save()
        c3 = Component(name="c3", mass="5 kg",
                       reference_specs=[e1]).save()
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
        for st in SourceTerm.objects(source__name="K40"):
            if st.component.id == c2.id and st.source.name == "K40":
                self.assertFalse(st.hiteffs_auto)
                self.assertEqual(len(st.hiteffs), 1)
                self.assertEqual(st.hiteffs[0].id, h4.id)

        # now add a new hiteff that would match our custom one
        h5 = HitEfficiency(source="K40", location="c2 placement location",
                           values=dict(v1=AsymmetricError(0.02, 0.002)*units('dru/mBq'))).save()
        h6 = HitEfficiency(source="K40", location="a1 location",
                           values=dict(v1=AsymmetricError(0.03, 0.002)*units('dru/mBq'))).save()
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
            val = cr.values['v1'].to('dru')
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
        val = cr.values['v1'].to('dru')
        self.assertAlmostEqual(val.mode, 101)
        self.assertAlmostEqual(val.s0, 10.903300234332724)
        self.assertAlmostEqual(val.s1, 13.212474184422296)






