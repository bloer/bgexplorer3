import unittest
from mongoengine import (connect, disconnect, StringField, ReferenceField,
                         CASCADE, NULLIFY, PULL, ValidationError)
from bgexplorer.models.component import Component, Placement, Assembly
from bgexplorer.models.common import units

class TestComponent(unittest.TestCase):
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

    def tearDown(self):
        pass

    def test1_surface_area(self):
        c1 = Component(name="c1", surface_area="10 cm**2")
        self.assertEqual(c1.outer_surface_area.m, 10)
        self.assertEqual(c1.inner_surface_area, 0)
        self.assertEqual(c1.outer_surface_area, c1.surface_area)

    def test2_assembly(self):
        c1 = Component(name="c1", mass="10 kg", surface_area="3 cm**2",
                       volume="5 cm**3").save()
        c2 = Component(name="c2", mass="30 kg", inner_surface_area="20 cm**2",
                       outer_surface_area="25 cm**2", volume="40 cm**3").save()
        a1 = Assembly(name="a1", components=(c1, c2)).save()

        self.assertEqual(Component.objects.count(), 3)
        self.assertEqual(Component.objects.no_sub_classes().count(), 2)
        self.assertEqual(Assembly.objects.count(), 1)

        self.assertEqual(a1.mass, 40*units.kg)
        self.assertEqual(a1.inner_surface_area, 20*units.cm**2)
        self.assertEqual(a1.outer_surface_area, 28*units.cm**2)
        self.assertEqual(a1.volume, 45*units.cm**3)

        a2 = Assembly(name="a2", children=[Placement(component=c1, weight=3),
                                           Placement(component=c2, weight=7)]).save()

        self.assertEqual(a2.mass, 240*units.kg)
        self.assertEqual(a2.inner_surface_area, 140*units.cm**2)
        self.assertEqual(a2.outer_surface_area, 184*units.cm**2)
        self.assertEqual(a2.volume, 295*units.cm**3)

        c2.delete()
        a1.reload()
        a2.reload()
        self.assertEqual(Component.objects.count(), 3)
        self.assertEqual(Component.objects.no_sub_classes().count(), 1)
        self.assertEqual(Assembly.objects.count(), 2)
        self.assertEqual(len(a1.children), 1)
        self.assertEqual(len(a2.children), 1)
        for p in a1.children:
            self.assertEqual(p.component.id, c1.id)

    def test_json(self):
        c1 = Component(name="c1", mass="10 kg")
        json = c1.to_json()
        c2 = Component.from_json(json)
        self.assertEqual(c1.id, c2.id)
        self.assertEqual(c1.mass, c2.mass)

    def test3_circular(self):
        """ Make sure circular references cause an error """
        a1 = Assembly(name="a1").save()
        a2 = Assembly(name="a2", components=[a1]).save()
        a3 = Assembly(name="a3", components=[a2]).save()
        a1.children.append(Placement(component=a3))
        with self.assertRaises(ValidationError):
            a1.save()

