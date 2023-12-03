import unittest
from mongoengine import (connect, disconnect, StringField, ReferenceField,
                         CASCADE, NULLIFY, PULL)
from bgexplorer.models.component import Component, Placement, Assembly
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.sourceterm import SourceTerm, CalculatedResults
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.common import units
from bgexplorer.models.asymmetric import AsymmetricError


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
        a1.save()
        st = SourceTerm.objects(assemblyPathStr="a1/c2").first()
        self.assertEqual(st.location, "c2 placement location")
