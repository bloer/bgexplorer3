""" The bill of materials: one row per assembly path """
import dataclasses
import numpy as np
from bson import ObjectId
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.budget import bom, table, table_columns
from bgexplorer.models.budget.bom import Link, RowKey
from bgexplorer.models.common import units as unitreg
from bgexplorer.models.component import (Component, Assembly, Placement,
                                         PurchaseInfo)
from bgexplorer.models.assay import Assay
from bgexplorer.models.common import PublicationInfo
from bgexplorer.models.cosmogenic import (ActivatedMaterial, ActivationPeriod,
                                          CosmogenicActivation,
                                          CosmogenicIsotope)
from bgexplorer.models.emissionspec import (EmissionSpec, EmissionSource,
                                            SourceCategory)
from bgexplorer.models.exposure import RadonExposure, RadonPeriod
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.settings import (get_settings, HitEffConfig,
                                        SpectrumROI)
from bgexplorer.models.sourceterm import SourceTerm
from bgexplorer.versioning import control as vc
from tests.test_app_components import AppTestCase


class TestBom(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        settings = get_settings('main')
        settings.hiteffdbconfig.display_scalars = dict(
            v1=HitEffConfig(display_name='Value', display_unit='mdru'))
        settings.save()
        self.hits = {
            (source, location): HitEfficiency(
                source=source, location=location,
                scalars=dict(v1=f'{value} +- 0.01 dru/mBq')).save()
            for source, location, value in [
                ('Th232', 'c1', 0.1), ('K40', 'c1', 0.2),
                ('Th232', 'c2', 0.3), ('Th232', 'c3', 0.5),
                ('Co60', 'c1', 0.4), ('Co60', 'c2', 0.6)]}
        self.assay = Assay(
            name='copper assay', category=SourceCategory.assay,
            publication=PublicationInfo(shortlabel='Smith 2020'),
            sources=[EmissionSource(name='Th232', rate='10 +- 1 mBq/kg'),
                     EmissionSource(name='K40', rate='<25 mBq/kg'),
                     EmissionSource(name='Co60', rate='2 +- 1 mBq/kg')]
        ).save()
        self.e2 = EmissionSpec(name='e2', sources=[
            EmissionSource(name='Th232', rate='1 +- 0.5 mBq/kg')]).save()
        self.c1 = Component(
            name='c1', mass='2 kg', location='c1', material='copper',
            description='a copper part', surface_area='5 cm**2',
            purchaseinfo=PurchaseInfo(vendor='Acme', partnum='X-1',
                                      link='https://example.com/x1'),
            specs=[self.assay]).save()
        self.c2 = Component(name='c2', mass='1 kg', location='c2',
                            material='steel', specs=[self.assay]).save()
        self.c3 = Component(name='c3', mass='1 kg', location='c3',
                            specs=[self.e2]).save()
        self.a1 = Assembly(name='a1', children=[
            Placement(component=self.c1, weight=2),
            Placement(component=self.c2, label='c2 label')]).save()
        self.a2 = Assembly(name='a2', children=[
            Placement(component=self.a1, weight=3),
            Placement(component=self.c3),
            Placement(component=self.c1, weight=4)]).save()
        self.p_a1, self.p_c3, self.p_c1b = (str(p.id)
                                            for p in self.a2.children)
        self.p_c1, self.p_c2 = (str(p.id) for p in self.a1.children)

    def row(self, result, *path):
        return next(r for r in result.rows if r.key.placement_ids == path)

    def test_rows_and_outline(self):
        result = bom(self.a2)
        self.assertEqual(
            [(r.outline, r.depth, r.cells['name'][0].text)
             for r in result.rows],
            [('', 0, 'a2'), ('1', 1, 'a1'), ('1.1', 2, 'c1'),
             ('1.2', 2, 'c2 label'), ('2', 1, 'c3'), ('3', 1, 'c1')])
        self.assertEqual([r.has_children for r in result.rows],
                         [True, True, False, False, False, False])
        # a plain component is a single row
        self.assertEqual([r.outline for r in bom(self.c1).rows], [''])

    def test_row_keys(self):
        result = bom(self.a2)
        keys = [r.key for r in result.rows]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(keys[0], RowKey((), str(self.a2.original_id)))
        # one component under two parents: distinct rows, same component
        first = self.row(result, self.p_a1, self.p_c1)
        second = self.row(result, self.p_c1b)
        self.assertEqual(first.key.component, second.key.component)
        self.assertEqual(first.key.component, str(self.c1.original_id))
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(first.key.placement_ids, (self.p_a1, self.p_c1))
        # identified by original_id, never the version's own _id
        self.assertEqual(self.row(result, self.p_a1).key.component,
                         str(self.a1.original_id))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.outline = 'x'

    def test_weights(self):
        result = bom(self.a2)
        self.assertEqual(
            [(r.cells['weight'], r.cells['total_weight'])
             for r in result.rows],
            [(None, 1), (3, 3), (2, 6), (1, 3), (1, 1), (4, 4)])

    def test_component_fields(self):
        result = bom(self.a2)
        c1 = self.row(result, self.p_a1, self.p_c1)
        self.assertEqual(c1.cells['description'], 'a copper part')
        self.assertEqual(c1.cells['material'], 'copper')
        self.assertEqual(c1.cells['mass'], 2)
        self.assertAlmostEqual(c1.cells['surface_area'], 5e-4)
        self.assertEqual(c1.cells['component_id'], str(self.c1.original_id))
        self.assertEqual(c1.cells['placement_id'], self.p_c1)
        self.assertEqual(c1.cells['name'], (
            Link('c1', 'component', str(self.c1.original_id)),))
        self.assertEqual(c1.cells['part'], (Link(
            'Acme / X-1', url='https://example.com/x1'),))
        # missing optional fields are empty, not errors
        c3 = self.row(result, self.p_c3)
        self.assertIsNone(c3.cells['material'])
        self.assertIsNone(c3.cells['description'])
        self.assertEqual(c3.cells['part'], ())
        self.assertEqual(c3.cells['assay'], ())
        # an assembly's mass is the saved total of its parts
        self.assertEqual(result.rows[0].cells['mass'],
                         self.a2.mass.to('kg').m)

    def test_part_without_link(self):
        self.c3.purchaseinfo = PurchaseInfo(vendor='Acme')
        self.c3.save()
        part = self.row(bom(self.a2), self.p_c3).cells['part']
        self.assertEqual(part, (Link('Acme'),))

    def test_assays(self):
        other = Assay(name='second', sources=[
            EmissionSource(name='Th232', rate='1 +- 1 mBq/kg')]).save()
        self.c2.attach(other)
        self.c2.save()
        result = bom(self.a2)
        c1 = self.row(result, self.p_a1, self.p_c1)
        self.assertEqual(c1.cells['assay'], (Link(
            'copper assay (Smith 2020)', 'emissionspec',
            str(self.assay.original_id)),))
        # one entry per assay, a plain emission spec is not an assay
        c2 = self.row(result, self.p_a1, self.p_c2)
        self.assertEqual([link.text for link in c2.cells['assay']],
                         ['copper assay (Smith 2020)', 'second'])
        self.assertEqual(self.row(result, self.p_c3).cells['assay'], ())

    def test_bulk_isotopes(self):
        result = bom(self.a2)
        c1 = self.row(result, self.p_a1, self.p_c1)
        self.assertIn('10', c1.cells['Th232'])
        self.assertIn('25', c1.cells['K40'])
        self.assertIsNone(c1.cells['U238'])
        self.assertIn('Co60', c1.cells['other_isotopes'])
        self.assertNotIn('Th232', c1.cells['other_isotopes'])
        self.assertIn('0.5', self.row(result, self.p_c3).cells['Th232'])

    def test_exposures(self):
        cu = ActivatedMaterial(name='Cu', material='copper', isotopes=[
            CosmogenicIsotope(isotope='Co60',
                              activationrate='97 +- 10 1/kg/day')]).save()
        cosmo = CosmogenicActivation(
            name='Cu nominal', material=cu,
            periods=[ActivationPeriod(duration='30 day'),
                     ActivationPeriod(duration='10 day', factor=0)]).save()
        radon = RadonExposure(name='cleanroom', periods=[
            RadonPeriod(radonlevel='10 +- 1 Bq/m**3',
                        duration='2 day')]).save()
        self.c3.attach(cosmo)
        self.c3.attach(radon)
        self.c3.save()
        result = bom(self.a2)
        c3 = self.row(result, self.p_c3)
        self.assertIn('Cu', c3.cells['cosmogenic'])
        self.assertIn('2 period', c3.cells['cosmogenic'])
        self.assertIn('40', c3.cells['cosmogenic'])
        self.assertIn('1 period', c3.cells['radon'])
        self.assertIn('Pb210', c3.cells['radon'])
        # the computed exposure sources are not bulk isotopes
        self.assertNotIn('Co60', c3.cells['other_isotopes'] or '')
        self.assertIsNone(self.row(result, self.p_c1b).cells['cosmogenic'])
        self.assertIsNone(self.row(result, self.p_c1b).cells['radon'])

    def test_columns(self):
        result = bom(self.a2)
        keys = [c.key for c in result.columns]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(keys[:2], ['outline', 'name'])
        by_key = {c.key: c for c in result.columns}
        for key in ('component_id', 'placement_id', 'surface_area',
                    'other_isotopes'):
            self.assertFalse(by_key[key].visible, key)
        for key in ('description', 'weight', 'total_weight', 'mass', 'part',
                    'assay', 'U238', 'Th232', 'K40', 'v1'):
            self.assertTrue(by_key[key].visible, key)
        self.assertEqual([(c.key, c.label) for c in result.columns
                          if c.group == 'contribution'],
                         [(k, n) for k, n in table_columns('main')])
        # every row has a cell for each column
        for row in result.rows:
            self.assertEqual(set(row.cells), set(keys) - {'outline'})

    def test_contributions_are_inclusive(self):
        result = bom(self.a2)
        value = lambda *path: self.row(result, *path).cells['v1'].m.nominal_value
        # a leaf is its own contribution; assemblies sum their subtree
        self.assertAlmostEqual(value(self.p_a1),
                               value(self.p_a1, self.p_c1) +
                               value(self.p_a1, self.p_c2))
        self.assertAlmostEqual(
            value(), value(self.p_a1) + value(self.p_c3) + value(self.p_c1b))
        self.assertGreater(value(self.p_c3), 0)
        # and agrees with the contributions table
        rows = table(self.a2)['rows']
        self.assertAlmostEqual(rows[0]['values']['v1'].m.nominal_value,
                               value())
        self.assertAlmostEqual(rows[1]['values']['v1'].m.nominal_value,
                               value(self.p_a1))
        self.assertEqual(str(self.row(result, self.p_c3).cells['v1'].u),
                         'mdru')

    def test_contributions_missing(self):
        # nothing to show for a component with no hit efficiencies
        HitEfficiency.objects.get(source='Th232', location='c3').delete()
        result = bom(self.a2)
        self.assertIsNone(self.row(result, self.p_c3).cells['v1'])
        self.assertIsNotNone(self.row(result, self.p_a1).cells['v1'])

    def test_rois(self):
        # a flat 1 dru/mBq/keV spectrum, so the 0-5 keV ROI is 5 dru/mBq
        for hiteff in HitEfficiency.select_version('main'):
            hiteff.spectra['s1'] = Histogram(
                AsymmetricUncertainty(np.ones(10), np.zeros(10))
                * unitreg('dru/mBq/keV'), np.arange(11) * unitreg.keV)
            hiteff.save()
        settings = get_settings('main')
        settings.hiteffdbconfig.rois = [
            SpectrumROI(spectrum='s1', start=0 * unitreg.keV,
                        stop=5 * unitreg.keV, mode='integrate',
                        display_name='low', display_unit='dru')]
        settings.save()
        result = bom(self.a2)
        self.assertEqual([c.key for c in result.columns
                          if c.group == 'contribution'], ['v1', 'low'])
        low = lambda *path: self.row(result, *path).cells['low'].m.nominal_value
        self.assertAlmostEqual(low(self.p_a1),
                               low(self.p_a1, self.p_c1) +
                               low(self.p_a1, self.p_c2))
        self.assertGreater(low(self.p_c3), 0)

    def test_hit_efficiencies(self):
        result = bom(self.a2)
        c1 = self.row(result, self.p_a1, self.p_c1)
        names = {link.text: link for link in c1.cells['hiteffs']
                 if isinstance(link, Link)}
        self.assertEqual(set(names), {'Th232 - c1', 'K40 - c1', 'Co60 - c1'})
        self.assertEqual(
            names['K40 - c1'],
            Link('K40 - c1', 'hitefficiency',
                 str(self.hits['K40', 'c1'].original_id)))
        # own terms only: the assembly has none of its own
        self.assertEqual(self.row(result, self.p_a1).cells['hiteffs'], ())
        # the same component placed elsewhere has the same ones
        other = self.row(result, self.p_c1b)
        self.assertEqual({l.text for l in other.cells['hiteffs']},
                         {'Th232 - c1', 'K40 - c1', 'Co60 - c1'})

    def test_hit_efficiencies_none(self):
        """ SourceTerms with no hit efficiency are flagged, not matched """
        HitEfficiency.objects.get(source='Th232', location='c3').delete()
        result = bom(self.a2)
        cell = self.row(result, self.p_c3).cells['hiteffs']
        self.assertEqual([c.text for c in cell], ['none for 1 term'])
        self.assertIsNone(cell[0].endpoint)

    def test_hit_efficiencies_from_sourceterms(self):
        """ What SourceTerms carry is what is shown, with no matching """
        bom(self.a2)
        term = SourceTerm.objects(assemblyRoot=self.a2,
                                  placement_ids=[ObjectId(self.p_c3)]).first()
        self.assertTrue(term.hiteffs)
        term.hiteffs = [self.hits['Th232', 'c1']]
        term.hiteffs_auto = False
        term.save()
        cell = self.row(bom(self.a2), self.p_c3).cells['hiteffs']
        self.assertEqual([c.text for c in cell], ['Th232 - c1'])

    def test_cache(self):
        result = bom(self.a2)
        self.assertIs(bom(self.a2), result)
        self.c1.mass = '3 kg'
        self.c1.save()
        updated = bom(self.a2)
        self.assertIsNot(updated, result)
        self.assertEqual(
            self.row(updated, self.p_a1, self.p_c1).cells['mass'], 3)
        self.assertEqual(
            self.row(result, self.p_a1, self.p_c1).cells['mass'], 2)

    def test_empty_assembly(self):
        empty = Assembly(name='empty').save()
        result = bom(empty)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].cells['mass'], 0)
        self.assertFalse(result.rows[0].has_children)
