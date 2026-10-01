""" Plot data: serializing results, and the pages drawing them """
import unittest
import numpy as np
from bgexplorer.application.plotting import (histogram_json, scalar_json,
                                             ONE_SIGMA_CL)
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.budget import (available_scalars, BudgetFilter,
                                      spectrum_breakdown)
from bgexplorer.models.common import units
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.sourceterm import CalculatedResults
from bgexplorer.models.hiteff import HitEfficiency
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
        self.assertEqual(h['is_limit'], [False] * 4)
        # empty bins are upper limits
        h = histogram_json(Histogram(
            AsymmetricUncertainty.fromcounts(np.array([0., 3., 0.]))
            * units('dru/mBq'), np.arange(4.) * units.keV))
        self.assertEqual(h['is_limit'], [True, False, True])
        self.assertGreater(h['upper_limit'][0], 0)
        self.assertEqual(h['upper_limit'][0], h['upper_limit'][2])
        self.assertEqual(h['value'][0], 0)
        # every bin's upper limit is its one-sided 1 sigma limit: a hit plus
        # a limit is never below the limit alone
        limit = AsymmetricUncertainty.fromlimit(np.array([3., 3., 3.]))
        hit = AsymmetricUncertainty(np.array([0., 0.01, 1.]),
                                    np.array([0., 0.003, 0.1]),
                                    np.array([0., 0.003, 0.1]))
        h = histogram_json(Histogram((limit + hit) * units('dru'),
                                     np.arange(4.) * units.keV))
        self.assertEqual(h['is_limit'], [True, False, False])
        self.assertAlmostEqual(h['upper_limit'][0],
                               AsymmetricUncertainty.fromlimit(3.).ppf(ONE_SIGMA_CL))
        self.assertGreaterEqual(h['upper_limit'][1], h['upper_limit'][0])
        self.assertGreater(h['upper_limit'][2], h['upper_limit'][1])
        # and for a plain measurement, value + err_plus
        h = histogram_json(spectrum())
        np.testing.assert_allclose(
            h['upper_limit'], np.add(h['value'], h['err_plus']))
        # plain values have no limits
        h = histogram_json(Histogram(np.array([0., 1.]), np.arange(3.)))
        self.assertEqual(h['is_limit'], [False, False])
        self.assertEqual(h['upper_limit'], [0., 1.])

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

    def assertSameHist(self, value, expected):
        np.testing.assert_allclose(value.hist.m.nominal_value,
                                   expected.hist.m.nominal_value)
        np.testing.assert_allclose(value.hist.m.ppf(0.9),
                                   expected.hist.m.ppf(0.9))

    def test_spectrum_breakdown(self):
        p_a1 = str(self.a2.children[0].id)
        result = spectrum_breakdown(self.a2, 's1')
        # c1's Th232 and K40 have it, c2 doesn't
        self.assertEqual((result['count'], result['ntotal']), (2, 2))
        self.assertSameHist(result['total'], CalculatedResults.for_object(
            self.a2, save=False, cache=False).spectra['s1'])
        self.assertEqual(result['curves'], [])
        self.assertIsNone(result['other'])
        self.assertIsNone(spectrum_breakdown(self.a2, 'nope')['total'])

        # a single group left over isn't "other"
        result = spectrum_breakdown(self.a2, 's1', groupby='isotope')
        curves = {c['key']: c for c in result['curves']}
        self.assertEqual(set(curves), {'Th232', 'K40'})
        self.assertIsNone(result['other'])
        # ranked by their (approximate) means, so the K40 limit is larger
        self.assertEqual([c['key'] for c in result['curves']],
                         ['K40', 'Th232'])
        self.assertEqual([c['rank'] for c in result['curves']], [0, 1])
        th = spectrum_breakdown(self.a2, 's1', BudgetFilter(
            dict(isotope=[('Th232', False)])), groupby='isotope')
        self.assertSameHist(th['total'], curves['Th232']['value'])
        # which keeps its unfiltered rank
        self.assertEqual([(c['key'], c['rank']) for c in th['curves']],
                         [('Th232', 1)])
        # the rest are summed
        top = spectrum_breakdown(self.a2, 's1', groupby='isotope', top=0)
        self.assertEqual(top['curves'], [])
        self.assertSameHist(top['other'], result['total'])

        result = spectrum_breakdown(self.a2, 's1', groupby='component')
        self.assertEqual([(c['key'], c['label']) for c in result['curves']],
                         [((p_a1,), 'a1')])
        result = spectrum_breakdown(self.a2, 's1', BudgetFilter(root=[p_a1]),
                                    groupby='component')
        self.assertEqual([c['label'] for c in result['curves']], ['c1'])
        with self.assertRaises(ValueError):
            spectrum_breakdown(self.a2, 's1', groupby='nope')

    def test_spectrum_json(self):
        url = self.url('component.spectrum_json', object=self.a2)
        data = self.client.get(url).get_json()
        self.assertEqual(data['spectra'], [dict(key='s1', name='s1')])
        self.assertEqual(data['spectrum'], 's1')
        self.assertEqual(len(data['total']['bins']), 5)
        self.assertEqual(data['total']['binsunit'], 'keV')
        self.assertEqual(data['curves'], [])
        data = self.client.get(url, query_string=dict(
            groupby='isotope', filters='{"isotope": [{"key": "K40"}]}'
        )).get_json()
        self.assertEqual((data['count'], data['ntotal']), (1, 2))
        self.assertEqual([(c['label'], c['rank']) for c in data['curves']],
                         [('K40', 0)])
        self.assertTrue(all(data['curves'][0]['value']['is_limit']))
        for query in (dict(spectrum='nope'), dict(groupby='nope'),
                      dict(filters='nope')):
            with self.subTest(query=query):
                response = self.client.get(url, query_string=query)
                self.assertEqual(response.status_code, 400)
                self.assertIn('message', response.get_json()['error'])

    def test_available_scalars(self):
        self.assertEqual(available_scalars('main'), ['v1'])

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
        page = self.html(self.client.get(self.url('component.results',
                                                  object=self.a1)))
        self.assertIn('>Results</a>', page)
        # contributions table, then budget, then spectra
        order = [page.index(text) for text in (
            'Background Contributions', 'id="budgetplot"',
            'id="spectrumplot"')]
        self.assertEqual(order, sorted(order))
        self.assertIn('<tr class="component depth1">', page)
        self.assertIn('bgplots.dashboard(', page)
        self.assertIn('bgplots.spectrumBreakdown(', page)
        self.assertIn('plotly-basic.min.js', page)
        # relative to a parent assembly
        response = self.client.get(self.url('component.results',
                                            object=self.a1, relativeto=self.a2))
        self.assertIn('as placed in a2', self.html(response))
        # the summary has neither
        page = self.html(self.client.get(self.url('component.view',
                                                  object=self.a1)))
        for text in ('Background Contributions', 'spectrumplot', 'budgetplot',
                     'plotly-basic.min.js', 'Bill of Materials', '>Budget<'):
            self.assertNotIn(text, page)
        self.assertIn('id="otherversions"', page)
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
