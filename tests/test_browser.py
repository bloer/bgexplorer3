""" Plots in a real (headless) browser.

Needs the optional dev tools `pip install playwright` and
`playwright install chromium`; the tests are skipped without them. If
Chromium's system libraries are installed somewhere unusual, point
BGEXPLORER_BROWSER_LIBS at them (it is added to the browser's
LD_LIBRARY_PATH).
"""
import os
import threading
import unittest
import numpy as np
from werkzeug.serving import make_server
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.common import units
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase

try:
    from playwright.sync_api import sync_playwright, Error as PlaywrightError
except ImportError:
    sync_playwright = None


def spectrum(counts):
    counts = np.asarray(counts, dtype=float)
    return Histogram(AsymmetricUncertainty.fromcounts(counts)
                     * units('dru/mBq'),
                     np.arange(len(counts) + 1.) * units.keV)


# what plotly actually draws, which can overflow its div
SPECTRUM_SVG = '#spectrumplot .main-svg >> nth=0'


class BrowserTestCase(AppTestCase):
    """ Serves the app on a local port and opens pages in Chromium """
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("playwright is not installed")
        super().setUpClass()
        env = dict(os.environ)
        if libs := os.environ.get('BGEXPLORER_BROWSER_LIBS'):
            env['LD_LIBRARY_PATH'] = ':'.join(
                filter(None, [libs, env.get('LD_LIBRARY_PATH')]))
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(env=env)
        except PlaywrightError as e:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Can't launch chromium: {e}")
        cls.server = make_server('127.0.0.1', 0, cls.app, threaded=True)
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.browser.close()
        cls.playwright.stop()
        super().tearDownClass()

    def open(self, url):
        """ Open `url` in a new page, failing the test on any JS error """
        page = self.browser.new_page(viewport=dict(width=1400, height=900))
        self.errors = []
        page.on('pageerror', lambda e: self.errors.append(str(e)))
        page.on('console', lambda m: m.type == 'error'
                and self.errors.append(f'{m.text} {m.location}'))
        self.addCleanup(page.close)
        self.addCleanup(lambda: self.assertEqual(self.errors, []))
        page.goto(self.base + url)
        return page

    def wait_plot(self, page, selector):
        page.locator(selector).scroll_into_view_if_needed()
        page.wait_for_selector(f'{selector} .js-plotly-plot', timeout=10000)
        page.wait_for_timeout(200)


class TestPlotsInBrowser(BrowserTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.hiteff = HitEfficiency(
            source='Th232', location='c1',
            scalars=dict(v1='0.1 +- 0.01 dru/mBq'),
            spectra=dict(s1=spectrum([1, 2, 0, 4, 5, 0]),
                         s2=spectrum([3, 3, 2, 1, 0, 0]))).save()
        HitEfficiency(source='K40', location='c2',
                      scalars=dict(v1='0.3 +- 0.03 dru/mBq')).save()
        e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='Th232', rate='10 +- 1 mBq/kg'),
            EmissionSource(name='K40', rate='<25 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='2 kg', location='c1',
                            material='copper', specs=[e1]).save()
        self.c2 = Component(name='c2', mass='1 kg', location='c2',
                            material='steel', specs=[e1]).save()
        self.a1 = Assembly(name='a1', children=[
            Placement(component=self.c1, weight=2),
            Placement(component=self.c2)]).save()

    def check_below(self, page, above, below):
        """ `below` is visible and entirely under `above` """
        top = page.locator(above).bounding_box()
        bottom = page.locator(below)
        self.assertTrue(bottom.is_visible(), f"{below} is hidden")
        box = bottom.bounding_box()
        self.assertGreaterEqual(box['y'], top['y'] + top['height'] - 1,
                                f"{below} overlaps {above}")

    def check_contains(self, page, outer, inner):
        """ `inner` is drawn within `outer`'s box, i.e. in the page flow """
        box = page.locator(outer).bounding_box()
        drawn = page.locator(inner).bounding_box()
        self.assertLessEqual(drawn['y'] + drawn['height'],
                             box['y'] + box['height'] + 1,
                             f"{inner} overflows {outer}")

    def switch_spectra(self, page):
        """ Change the selection and scales, checking the layout each time """
        options = page.locator('#spectrumplot select option')
        # a plain click selects only s2, a ctrl-click adds s1
        for step, modifiers in ((1, []), (0, ['Control'])):
            options.nth(step).click(modifiers=modifiers)
            page.wait_for_timeout(200)
            yield step
        for toggle in ('#spectrumplot_logx', '#spectrumplot_logy'):
            page.locator(toggle).click()
            page.wait_for_timeout(200)
            yield toggle

    def test_hiteff_spectra_layout(self):
        page = self.open(self.url('hitefficiency.view', object=self.hiteff))
        self.wait_plot(page, '#spectrumplot')
        self.check_below(page, SPECTRUM_SVG, '#otherversions')
        for step in self.switch_spectra(page):
            with self.subTest(step=step):
                self.check_contains(page, '#spectrumplot', SPECTRUM_SVG)
                self.check_below(page, SPECTRUM_SVG, '#otherversions')

    def test_component_spectra_layout(self):
        page = self.open(self.url('component.results', object=self.c1))
        self.wait_plot(page, '#spectrumplot')
        for step in self.switch_spectra(page):
            with self.subTest(step=step):
                self.check_contains(page, '#spectrumplot', SPECTRUM_SVG)

    def ranges(self, page):
        """ The x range of each budget panel, None for ones without a plot """
        return page.evaluate("""() => document.getElementById('budgetplot')
            .panels.map(p => p._fullLayout ? p._fullLayout.xaxis.range : null)""")

    def test_budget_shared_axis(self):
        page = self.open(self.url('component.results', object=self.a1))
        page.locator('#budgetplot').scroll_into_view_if_needed()
        page.wait_for_function("""() => {
            const div = document.getElementById('budgetplot');
            return div.panels && div.panels.every(p => p._fullLayout);}""",
            timeout=10000)
        full = self.ranges(page)
        self.assertEqual(len(full), 3)
        for r in full[1:]:
            self.assertEqual(r, full[0])
        # covers every row: 2 kg x 2 x 10 mBq/kg x 0.1 dru/mBq = 4 +- 0.6 dru
        # from c1's Th232, and c2's K40 limit, 1 kg x 25 mBq/kg x 0.3 dru/mBq
        low, high = full[0]
        self.assertLess(low, np.log10(3.4))
        self.assertGreater(high, np.log10(7.5))

        # hiding rows keeps the range
        page.fill('#budgetplot_threshold', '50')
        page.dispatch_event('#budgetplot_threshold', 'change')
        page.wait_for_timeout(500)
        rows = page.evaluate("""() => document.getElementById('budgetplot')
            .panels.map(p => p._fullLayout ? p.data[2].y.length : 0)""")
        self.assertLess(min(rows), 2)
        for r in self.ranges(page):
            if r is not None:
                self.assertEqual(r, full[0])

        # zooming one panel zooms the others
        page.evaluate("""() => Plotly.relayout(
            document.getElementById('budgetplot').panels[0],
            {'xaxis.range[0]': 0, 'xaxis.range[1]': 0.5})""")
        page.wait_for_timeout(500)
        for r in self.ranges(page):
            if r is not None:
                self.assertEqual(r, [0, 0.5])

    def test_limit_bins(self):
        page = self.open(self.url('hitefficiency.view', object=self.hiteff))
        self.wait_plot(page, '#spectrumplot')
        traces = page.evaluate("""() => document.querySelector(
            '#spectrumplot .js-plotly-plot').data.map(t => ({
                name: t.name, x: t.x, y: t.y,
                symbol: t.marker && t.marker.symbol}))""")
        # s1 = [1, 2, 0, 4, 5, 0]: bins 2 and 5 are limits
        (markers,) = [t for t in traces if t['symbol'] == 'triangle-down']
        self.assertEqual(markers['x'], [2.5, 5.5])
        self.assertTrue(all(y > 0 for y in markers['y']))
        (line,) = [t for t in traces if t['name'] == 's1']
        # each measured bin is a full step, broken at the limits
        self.assertEqual(line['x'], [0, 1, 1, 2, None, 3, 4, 4, 5, None])
        self.assertEqual(line['y'][:4], [1, 1, 2, 2])
