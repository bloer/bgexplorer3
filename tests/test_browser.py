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
from unittest import mock
import numpy as np
from werkzeug.serving import make_server
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.common import units
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.cosmogenic import ActivatedMaterial, CosmogenicIsotope
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.users import User, Role
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase
from tests.test_radiopurity import fixture

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


class TestEmissionSpecEditor(BrowserTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.spec = EmissionSpec(name='e1', sources=[
            EmissionSource(name='U238', rate='10 +- 1 mBq/kg')]).save()

    def test_override_badge(self):
        """ editing a generated source marks it as an override """
        page = self.open(self.url('emissionspec.edit', object=self.spec))
        rows = page.locator('#sources tbody tr')
        self.assertEqual(rows.count(), 3)
        self.assertEqual(page.locator('.badge.generated').count(), 2)
        ra226 = rows.nth(2)
        self.assertIn('default (from U238)', ra226.inner_text())
        ra226.locator('input[name="sources.rate"]').fill('4 mBq/kg')
        self.assertEqual(ra226.locator('.badge').inner_text(), 'override')
        self.assertEqual(page.locator('.badge.generated').count(), 1)
        # a select counts as an edit too
        u235 = rows.nth(1)
        u235.locator('select[name="sources.category"]').select_option('radon')
        self.assertEqual(page.locator('.badge.generated').count(), 0)
        # adding a row works, and has no badge
        page.click('button[data-tableid="sources"]')
        self.assertEqual(rows.count(), 4)
        self.assertEqual(rows.nth(3).locator('.badge').count(), 0)


class TestReferencePicker(BrowserTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.cu = ActivatedMaterial(name='Cu', isotopes=[CosmogenicIsotope(
            isotope='Co60', activationrate='97 1/kg/day')]).save()
        self.c1 = Component(name='c1', mass='1 kg').save()

    def material(self):
        return Component.objects.get(name='c1').activated_material

    def test_choose_and_clear(self):
        page = self.open(self.url('component.edit', object=self.c1))
        picker = page.locator('#activated_material')
        self.assertEqual(picker.locator('.referencename').inner_text(),
                         'none')
        picker.locator('button.referenceSelector').click()
        # bootstrap ignores hide() until the modal has finished opening
        page.wait_for_function("document.getElementById("
                               "'selectReferenceModal').classList"
                               ".contains('show')")
        page.wait_for_timeout(500)
        page.locator('#selectReferenceModalBody a:text-is("Cu")').click()
        page.wait_for_selector('#selectReferenceModal', state='hidden')
        self.assertEqual(picker.locator('.referencename').inner_text(), 'Cu')
        page.click('#mainform button[type=submit] >> nth=0')
        page.wait_for_url(self.base + self.url('component.view',
                                               object=self.c1))
        self.assertEqual(self.material().name, 'Cu')

        page.goto(self.base + self.url('component.edit', object=self.c1))
        self.assertEqual(picker.locator('.referencename').inner_text(), 'Cu')
        picker.locator('button.referenceClear').click()
        self.assertEqual(picker.locator('.referencename').inner_text(),
                         'none')
        page.click('#mainform button[type=submit] >> nth=0')
        page.wait_for_url(self.base + self.url('component.view',
                                               object=self.c1))
        self.assertIsNone(self.material())


class TestRadiopuritySpinner(BrowserTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        # hold the search open until the test has seen the spinner
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        def post(*args, **kwargs):
            self.release.wait(10)
            return mock.Mock(text=fixture('mumetal'))
        patcher = mock.patch('bgexplorer.models.radiopurity.requests.post',
                             side_effect=post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def submit(self, page, button):
        """ Click `button`, and return the spinner's state as the form is
        submitted. It's reported from the submit event, since the page may
        not be readable once it is navigating
        """
        states = []
        name = f'report{id(states)}'
        page.expose_function(name, lambda state: states.append(state))
        # added after the page's own listener, so runs after it
        page.evaluate("""([button, name]) => {
            document.querySelector(button).form.addEventListener(
                'submit', () => window[name]({
                    shown: getComputedStyle(
                        document.getElementById('searching'))
                        .display !== 'none',
                    message: document.getElementById('searchingmessage')
                        .textContent,
                    disabled: document.querySelector(button).disabled}));
        }""", [button, name])
        page.click(button, no_wait_after=True)
        while not states:
            page.wait_for_timeout(50)
        return states[0]

    def test_spinner(self):
        page = self.open(self.url('radiopurity.search'))
        self.assertFalse(page.locator('#searching').is_visible())
        page.fill('#q', 'mumetal')
        state = self.submit(page, '#searchform button[type=submit]')
        self.assertEqual(state, dict(
            shown=True, disabled=True,
            message='Searching radiopurity.org\u2026'))
        self.release.set()
        page.wait_for_selector('#importform')
        self.assertFalse(page.locator('#searching').is_visible())
        self.assertTrue(page.locator('#searchform button[type=submit]')
                        .is_enabled())

        self.release.clear()
        state = self.submit(page, '#importselected')
        self.assertEqual(state['message'],
                         'Importing from radiopurity.org\u2026')
        self.assertTrue(state['shown'] and state['disabled'])
        self.release.set()
        page.wait_for_url(self.base + self.url('emissionspec.overview'))


class TestLoginInBrowser(BrowserTestCase):
    LOGIN_DISABLED = False

    def setUp(self):
        super().setUp()
        User.drop_collection()
        vc.create_version('main')
        self.c1 = Component(name='c1', mass='1 kg').save()
        user = User(name='u1', role=Role.editor)
        user.set_password('correct horse')
        user.save()

    def test_login(self):
        view = self.url('component.view', object=self.c1)
        page = self.open(view)
        self.assertEqual(page.locator('a:text-is("Edit")').count(), 0)
        page.click('#loginlink')
        page.fill('#username', 'u1')
        page.fill('#password', 'wrong password')
        page.click('#loginform button[type=submit]')
        self.assertTrue(page.locator('#loginerror').is_visible())
        # a failed login is a 401, which the browser reports as an error
        self.errors = [e for e in self.errors if '401' not in e]
        page.fill('#password', 'correct horse')
        page.click('#loginform button[type=submit]')
        page.wait_for_url(self.base + view)
        self.assertEqual(page.locator('#profilelink').inner_text(), 'u1')
        page.click('a:text-is("Edit")')
        page.wait_for_url(self.base + self.url('component.edit',
                                               object=self.c1))
        page.click('#logoutform button')
        self.assertTrue(page.locator('#loginlink').is_visible())
