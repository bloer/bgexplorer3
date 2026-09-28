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
                self.check_below(page, SPECTRUM_SVG, '#otherversions')

    def test_component_spectra_layout(self):
        page = self.open(self.url('component.view', object=self.c1))
        self.wait_plot(page, '#spectrumplot')
        for step in self.switch_spectra(page):
            with self.subTest(step=step):
                self.check_below(page, SPECTRUM_SVG, '#otherversions')
