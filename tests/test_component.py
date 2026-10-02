import unittest
from mongoengine import (connect, disconnect, StringField, ReferenceField,
                         CASCADE, NULLIFY, PULL, ValidationError)
from bgexplorer.models.component import (Component, Placement, Assembly,
                                         LocationOverride)
from bgexplorer.models.common import units
from tests.dbutil import connect_test_db

class TestComponent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

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

    def test4_children_versions(self):
        """ Placements should resolve to the child in the same version """
        tag = Component.get_default_tag()
        c1 = Component(name="c1", mass="10 kg").save()
        Assembly(name="a1", components=[c1]).save()
        Component.create_tag('v1')
        c1 = Component.select_tag(tag).get(name="c1")
        c1.mass = 20 * units.kg
        c1.save()
        a1v1 = Assembly.select_tag('v1').get(name="a1")
        a1main = Assembly.select_tag(tag).get(name="a1")
        self.assertEqual(a1v1.children[0].component.mass, 10 * units.kg)
        self.assertEqual(a1main.children[0].component.mass, 20 * units.kg)

    def test5_unique_ids(self):
        c1 = Component(name="c1").save()
        p = Placement(component=c1)
        with self.assertRaises(ValidationError):
            Assembly(name="a1", children=[p, Placement(id=p.id, component=c1)]
                     ).save()
        o = LocationOverride(source="K40", location="x")
        with self.assertRaises(ValidationError):
            Component(name="c2", location_overrides=[o, LocationOverride(
                id=o.id, source="U238", location="y")]).save()

