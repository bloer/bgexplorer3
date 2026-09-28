""" Plot data: serializing results and budget breakdowns """
import unittest
import numpy as np
from bgexplorer.application.plotting import histogram_json, scalar_json
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.budget import budget_breakdown, available_scalars
from bgexplorer.models.common import units
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.sourceterm import CalculatedResults
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase


def spectrum(scale=1.):
    return Histogram(AsymmetricUncertainty.fromcounts(scale * np.arange(1., 5.))
                     * units('dru/mBq'), np.arange(5.) * units.keV)


class TestSerialize(unittest.TestCase):
    def test_histogram(self):
        h = histogram_json(spectrum())
        self.assertEqual(h['bins'], [0., 1., 2., 3., 4.])
        self.assertEqual(h['value'], [1., 2., 3., 4.])
        self.assertEqual(len(h['err_minus']), 4)
        self.assertTrue(all(e > 0 for e in h['err_plus']))
        self.assertEqual(h['units'], 'dru/mBq')
        self.assertEqual(h['binsunit'], 'keV')
        # converted to a display unit
        h = histogram_json(spectrum(), units('dru/Bq').u)
        self.assertEqual(h['value'], [1000., 2000., 3000., 4000.])
        self.assertEqual(h['units'], 'dru/Bq')

    def test_scalar(self):
        q = units.Quantity(AsymmetricUncertainty(2, 0.5, 1), 'mBq')
        s = scalar_json(q, 'Bq')
        self.assertAlmostEqual(s['value'], 0.002)
        self.assertAlmostEqual(s['err_minus'], 0.0005)
        self.assertAlmostEqual(s['err_plus'], 0.001)
        self.assertFalse(s['is_limit'])
        self.assertEqual(s['units'], 'Bq')
        limit = scalar_json(units.Quantity(
            AsymmetricUncertainty.fromlimit(3.), 'mBq'))
        self.assertTrue(limit['is_limit'])
        self.assertAlmostEqual(limit['upper_limit'], 3.)
        self.assertEqual(limit['value'], 0)
        self.assertIsNone(scalar_json(None))


class TestPlots(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        HitEfficiency(source='Th232', location='c1',
                      scalars=dict(v1='0.1 +- 0.01 dru/mBq'),
                      spectra=dict(s1=spectrum())).save()
        HitEfficiency(source='K40', location='c1',
                      scalars=dict(v1='0.2 +- 0.02 dru/mBq'),
                      spectra=dict(s1=spectrum(2))).save()
        HitEfficiency(source='Th232', location='c2',
                      scalars=dict(v1='0.3 +- 0.03 dru/mBq')).save()
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='Th232', rate='10 +- 1 mBq/kg'),
            EmissionSource(name='K40', rate='<25 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='2 kg', location='c1',
                            material='copper', specs=[self.e1]).save()
        self.c2 = Component(name='c2', mass='1 kg', location='c2',
                            material='steel', specs=[self.e1]).save()
        self.a1 = Assembly(name='a1', children=[
            Placement(component=self.c1, weight=2),
            Placement(component=self.c2, label='c2 label')]).save()
        self.a2 = Assembly(name='a2', children=[
            Placement(component=self.a1)]).save()

    def total(self, obj, relativeto=None):
        return CalculatedResults.for_object(obj, relativeto, save=False,
                                            cache=False).scalars['v1']

    def check_sum(self, budget, total):
        """ All groups add up to the total """
        parts = [v for row in budget['rows']
                 for v in (row['measured'], row['limit']) if v is not None]
        summed = sum(parts[1:], parts[0]).to(total.u)
        self.assertAlmostEqual(summed.m.nominal_value, total.m.nominal_value)
        self.assertAlmostEqual(summed.m.get_upper_limit(0.9),
                               total.m.get_upper_limit(0.9))

    def test_budget_groups(self):
        total = self.total(self.a1)
        budget = budget_breakdown(self.a1, 'v1', 'component')
        rows = {row['label']: row for row in budget['rows']}
        self.assertEqual(set(rows), {'c1', 'c2 label'})
        # c1: 2 x 2 kg x 10 mBq/kg x 0.1 dru/mBq, and a limit from K40
        self.assertAlmostEqual(rows['c1']['measured'].m.nominal_value, 4.)
        self.assertTrue(rows['c1']['limit'].m.isupperlimit())
        # c2: 1 kg x 10 mBq/kg x 0.3 dru/mBq, no hiteff for K40
        self.assertAlmostEqual(rows['c2 label']['measured'].m.nominal_value,
                               3.)
        self.assertIsNone(rows['c2 label']['limit'])
        self.assertEqual(budget['rows'][0]['label'], 'c1')
        self.check_sum(budget, total)

        budget = budget_breakdown(self.a1, 'v1', 'isotope')
        rows = {row['label']: row for row in budget['rows']}
        self.assertEqual(set(rows), {'Th232', 'K40'})
        self.assertIsNone(rows['Th232']['limit'])
        self.assertAlmostEqual(rows['Th232']['measured'].m.nominal_value, 7.)
        self.assertIsNone(rows['K40']['measured'])
        self.check_sum(budget, total)

        budget = budget_breakdown(self.a1, 'v1', 'material')
        self.assertEqual({row['label'] for row in budget['rows']},
                         {'copper', 'steel'})
        self.check_sum(budget, total)

    def test_budget_nested(self):
        # a2's only child is a1
        budget = budget_breakdown(self.a2, 'v1', 'component')
        self.assertEqual([row['label'] for row in budget['rows']], ['a1'])
        self.check_sum(budget, self.total(self.a2))
        # a1 relative to a2 splits into a1's children
        budget = budget_breakdown(self.a1, 'v1', 'component',
                                  relativeto=self.a2)
        self.assertEqual({row['label'] for row in budget['rows']},
                         {'c1', 'c2 label'})
        self.check_sum(budget, self.total(self.a1, self.a2))
        # a plain component's own sources
        budget = budget_breakdown(self.c1, 'v1', 'component')
        self.assertEqual([row['label'] for row in budget['rows']], ['c1'])
        self.check_sum(budget, self.total(self.c1))

    def test_budget_units_and_errors(self):
        budget = budget_breakdown(self.a1, 'v1', 'isotope', unit='mdru')
        self.assertEqual(budget['units'], units('mdru').u)
        rows = {row['label']: row for row in budget['rows']}
        self.assertAlmostEqual(rows['Th232']['measured'].m.nominal_value,
                               7000.)
        self.assertEqual(available_scalars('main'), ['v1'])
        with self.assertRaises(ValueError):
            budget_breakdown(self.a1, 'nope', 'isotope')
        with self.assertRaises(ValueError):
            budget_breakdown(self.a1, 'v1', 'nope')

    def test_budget_json(self):
        url = self.url('component.budget_json', object=self.a1)
        data = self.client.get(url + '?groupby=isotope').get_json()
        self.assertEqual(data['scalar'], 'v1')
        self.assertEqual(data['scalars'], ['v1'])
        self.assertEqual(data['units'], 'dru')
        rows = {row['label']: row for row in data['rows']}
        self.assertAlmostEqual(rows['Th232']['measured']['value'], 7.)
        self.assertFalse(rows['Th232']['measured']['is_limit'])
        self.assertTrue(rows['K40']['limit']['is_limit'])
        self.assertGreater(rows['K40']['limit']['upper_limit'], 0)
        # defaults to groupby component
        data = self.client.get(url).get_json()
        self.assertEqual(data['groupby'], 'component')
        for query in ('?groupby=nope', '?scalar=nope', '?unit=kg'):
            with self.subTest(query=query):
                response = self.client.get(url + query)
                self.assertEqual(response.status_code, 400)
                self.assertIn('message', response.get_json()['error'])
        # relative to a parent assembly
        data = self.client.get(self.url(
            'component.budget_json', object=self.a1,
            relativeto=self.a2)).get_json()
        self.assertEqual(len(data['rows']), 2)

    def test_spectra_json(self):
        hiteff = HitEfficiency.select_version('main').get(source='Th232',
                                                          location='c1')
        data = self.client.get(self.url('hitefficiency.spectra_json',
                                        object=hiteff)).get_json()
        self.assertEqual(list(data), ['s1'])
        self.assertEqual(data['s1']['value'], [1., 2., 3., 4.])
        self.assertEqual(data['s1']['units'], 'dru/mBq')

        data = self.client.get(self.url('component.spectra_json',
                                        object=self.c1)).get_json()
        s1 = data['s1']
        self.assertEqual(len(s1['bins']), len(s1['value']) + 1)
        # 2 kg x 10 mBq/kg x spectrum
        self.assertAlmostEqual(s1['value'][0], 20.)
        self.assertEqual(s1['binsunit'], 'keV')
        # a component without spectra
        self.assertEqual(self.client.get(self.url(
            'component.spectra_json', object=self.c2)).get_json(), {})

    def test_pages(self):
        page = self.html(self.client.get(self.url('component.budget',
                                                  object=self.a1)))
        self.assertIn('id="budgetplot"', page)
        self.assertIn('bgplots.budget(', page)
        self.assertIn('plotly-basic.min.js', page)
        page = self.html(self.client.get(self.url('component.view',
                                                  object=self.a1)))
        self.assertIn('id="spectrumplot"', page)
        self.assertIn('bgplots.spectrum(', page)
        self.assertNotIn('Bill of Materials', page)
        hiteff = HitEfficiency.select_version('main').get(source='K40')
        page = self.html(self.client.get(self.url('hitefficiency.view',
                                                  object=hiteff)))
        self.assertIn('id="spectrumplot"', page)
        # the source terms tab doesn't draw plots
        page = self.html(self.client.get(self.url('hitefficiency.sourceterms',
                                                  object=hiteff)))
        self.assertNotIn('bgplots.spectrum(', page)
        for name in ('plotly-basic.min.js', 'bgplots.js'):
            response = self.client.get(f'/static/{name}')
            self.assertEqual(response.status_code, 200)
            response.close()
