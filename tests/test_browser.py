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
from bgexplorer.application.plotting import ONE_SIGMA_CL
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

    def wait_spectrum(self, page):
        page.wait_for_function("""() => {
            const div = document.getElementById('spectrumplot');
            return div.plot && !div.classList.contains('loading')
                && (div.plot._fullLayout || div.plot.textContent);}""",
            timeout=10000)
        page.wait_for_timeout(200)

    def spectrum_names(self, page):
        """ The names of the curves in the spectrum's legend """
        return page.evaluate("""() => document.getElementById('spectrumplot')
            .plot.data.filter(t => t.showlegend !== false).map(t => t.name)""")

    def test_component_spectra_layout(self):
        page = self.open(self.url('component.results', object=self.a1))
        self.wait_plot(page, '#spectrumplot')
        self.wait_spectrum(page)
        self.assertEqual(self.spectrum_names(page), ['Total'])
        steps = [('#spectrumplot_spectrum', 's2'),
                 ('#spectrumplot_groupby', 'isotope'),
                 ('#spectrumplot_logx', None), ('#spectrumplot_logy', None)]
        for selector, value in steps:
            with self.subTest(step=selector):
                if value is None:
                    page.locator(selector).click()
                else:
                    page.select_option(selector, value)
                self.wait_spectrum(page)
                self.check_contains(page, '#spectrumplot', SPECTRUM_SVG)
        # only c1's Th232 has spectra
        self.assertEqual(self.spectrum_names(page), ['Total', 'Th232'])

    def test_spectrum_filters(self):
        a2 = Assembly(name='a2', children=[Placement(component=self.a1)]).save()
        page = self.open_dashboard(self.url('component.results', object=a2))
        page.select_option('#spectrumplot_groupby', 'material')
        self.wait_spectrum(page)
        self.assertEqual(self.spectrum_names(page), ['Total', 'copper'])
        # the spectrum follows the dashboard's filters
        self.click_row(page, 'material', 'copper')
        self.wait_spectrum(page)
        self.assertEqual(self.spectrum_names(page),
                         ['Filtered total', 'copper'])
        # c2's steel has no spectra
        self.click_row(page, 'material', 'steel')
        self.wait_spectrum(page)
        self.assertIn('Nothing passing the filters',
                      page.locator('#spectrumplot').inner_text())
        self.click_row(page, 'material', 'steel')
        self.wait_spectrum(page)
        self.assertEqual(self.spectrum_names(page), ['Total', 'copper'])

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
        self.assertEqual(len(full), 4)
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
            .panels.map(p => p._fullLayout ? p.data[p.data.length-1].y.length : 0)""")
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

    def open_dashboard(self, url):
        page = self.open(url)
        page.locator('#budgetplot').scroll_into_view_if_needed()
        self.wait_dashboard(page)
        return page

    def wait_dashboard(self, page):
        page.wait_for_function("""() => {
            const div = document.getElementById('budgetplot');
            return div.panels && !div.classList.contains('loading')
                && div.panels.every(p => p._fullLayout || p.textContent);}""",
            timeout=10000)

    def panel_js(self, groupby):
        return (f"document.querySelector("
                f"'#budgetplot .bgplot[data-groupby=\"{groupby}\"]')")

    def rows(self, page, groupby):
        """ The labels shown in a panel """
        return page.evaluate(f"""() => {self.panel_js(groupby)}
            ._fullLayout.yaxis.categoryarray.map(
                label => label.replace(/\\u200b/g, ''))""")

    def values(self, page, groupby):
        """ The labels of the rows with something drawn, in order """
        shown = page.evaluate(f"""() => {self.panel_js(groupby)}
            .data.map(t => t.y).flat()""")
        return [row for row in self.rows(page, groupby) if row in shown]

    def click_row(self, page, groupby, label, modifiers=(), wait=True,
                  drill=False):
        """ Click a row's y axis label, or the drill down marker after it """
        x, y = page.evaluate(f"""([label, drill]) => {{
            const p = {self.panel_js(groupby)};
            const i = p._fullLayout.yaxis.categoryarray.findIndex(
                l => l.replace(/\\u200b/g, '') === label);
            const tick = [...p.querySelectorAll('.ytick')].find(
                t => t.__data__.x === i);
            if(!tick)
                throw new Error('no row ' + label);
            let target = tick.querySelector('text');
            if(drill)
                target = [...tick.querySelectorAll('tspan')].find(
                    t => t.dataset.drill);
            const box = target.getBoundingClientRect();
            return [box.left + box.width / 2, box.top + box.height / 2];}}""",
            [label, drill])
        for key in modifiers:
            page.keyboard.down(key)
        page.mouse.move(x, y)
        page.mouse.click(x, y)
        if wait:
            for key in modifiers:
                page.keyboard.up(key)
            page.wait_for_timeout(100)
            self.wait_dashboard(page)

    def assertDrawn(self, page):
        """ Once any animation settles, every panel draws exactly its
        data: one marker and error bar per point, in its row, and no
        leftovers
        """
        page.wait_for_function("""() => document.getElementById('budgetplot')
            .panels.every(p => !p._fullLayout || [...p.querySelectorAll(
                '.scatterlayer .trace')].every(g => {
                const trace = p.data[g.__data__[0].trace.index];
                const n = trace.x.length, ya = p._fullLayout.yaxis;
                const points = [...g.querySelectorAll('.point')];
                return points.length === n
                    && g.querySelectorAll('.errorbar path.xerror').length === n
                    && points.every(pt => Math.abs(
                        +pt.getAttribute('transform').split(',')[1].replace(')', '')
                        - ya.l2p(p._rowids.indexOf(trace.y[pt.__data__.i]))) < 2);
            }))""", timeout=3000)

    def table_rows(self, page):
        """ The component column of the contributions table, once it has
        caught up with the filters
        """
        page.wait_for_function("""() => !document.getElementById('resultstable')
            .classList.contains('loading')""", timeout=5000)
        return page.locator('#resultstable tbody td:first-child').all_inner_texts()

    def yranges(self, page):
        return page.evaluate("""() => document.getElementById('budgetplot')
            .panels.map(p => p._fullLayout.yaxis.range)""")

    def info(self, page):
        return page.locator('#budgetplot .dashboard-info').inner_text()

    def test_dashboard_filters(self):
        a2 = Assembly(name='a2', children=[Placement(component=self.a1)]).save()
        url = self.url('component.results', object=a2)
        page = self.open_dashboard(url)
        self.assertIn('2 of 2 source terms', self.info(page))
        yranges = self.yranges(page)
        self.assertEqual(self.rows(page, 'component'), ['a1', 'c2', 'c1'])
        self.assertEqual(self.rows(page, 'material'), ['steel', 'copper'])
        self.assertEqual(self.table_rows(page), ['a2', 'a1', 'c1', 'c2'])
        table = page.locator('#resultstable')
        self.assertNotIn('budget filters', table.inner_text())

        self.assertIn('Total:', self.info(page))
        self.assertNotIn('Unfiltered', self.info(page))

        # clicking inside a plot doesn't filter
        box = page.evaluate(f"""() => {{
            const p = {self.panel_js('material')}, fl = p._fullLayout;
            const box = p.getBoundingClientRect();
            return [box.left + fl._size.l + fl._size.w / 2,
                    box.top + fl._size.t + fl.yaxis.l2p(1)];}}""")
        page.mouse.click(*box)
        page.wait_for_timeout(200)
        self.assertIn('2 of 2 source terms', self.info(page))

        # keep only copper: the other panels only show c1's Th232
        self.click_row(page, 'material', 'copper')
        self.assertIn('1 of 2 source terms', self.info(page))
        self.assertIn('copper', self.info(page))
        self.assertIn('Filtered total:', self.info(page))
        self.assertIn('Unfiltered total:', self.info(page))
        self.assertEqual(self.values(page, 'isotope'), ['Th232'])
        # the rows don't move, though K40's is now empty
        self.assertDrawn(page)
        self.assertEqual(self.yranges(page), yranges)
        # with the same rows
        self.assertEqual(self.rows(page, 'isotope'), ['K40', 'Th232'])
        # but its own panel still shows both, with copper shaded
        self.assertEqual(self.rows(page, 'material'), ['steel', 'copper'])
        shapes = page.evaluate(
            f"() => {self.panel_js('material')}.layout.shapes.length")
        self.assertEqual(shapes, 1)
        self.assertIn('copper', page.evaluate('() => location.hash'))
        # the table follows
        self.table_rows(page)
        self.assertIn('Only the 1 of 2 source terms', table.inner_text())

        # ctrl-click removes copper instead
        self.click_row(page, 'material', 'copper', ['Control'])
        self.assertIn('not copper', self.info(page))
        self.assertEqual(self.values(page, 'isotope'), ['K40'])
        self.assertDrawn(page)

        page.locator('#budgetplot .dashboard-reset').click()
        self.wait_dashboard(page)
        self.assertIn('2 of 2 source terms', self.info(page))
        self.assertDrawn(page)
        self.table_rows(page)
        self.assertNotIn('budget filters', table.inner_text())
        self.assertEqual(self.rows(page, 'isotope'), ['K40', 'Th232'])

        # shift-clicks are applied together when shift is released
        self.click_row(page, 'isotope', 'Th232', ['Shift'], wait=False)
        self.click_row(page, 'isotope', 'K40', wait=False)
        self.assertIn('Release shift', self.info(page))
        page.keyboard.up('Shift')
        self.wait_dashboard(page)
        self.assertNotIn('Release shift', self.info(page))
        self.assertIn('2 of 2 source terms', self.info(page))
        self.assertIn('K40', self.info(page))
        self.assertIn('Th232', self.info(page))
        page.locator('#budgetplot .dashboard-reset').click()
        self.wait_dashboard(page)

        # drill into an assembly from the marker before its label
        self.assertEqual(page.evaluate(f"""() => [...{self.panel_js('component')}
            .querySelectorAll('.ytick tspan title')].map(t => t.textContent)"""),
            ['drill down'])
        self.click_row(page, 'component', 'a1', drill=True)
        self.assertEqual(self.rows(page, 'component'), ['c2', 'c1'])
        self.assertEqual(self.table_rows(page), ['a1', 'c1', 'c2'])
        crumbs = page.locator('#budgetplot .breadcrumb')
        self.assertEqual(crumbs.inner_text().split(), ['a2', 'a1'])
        # and back out
        crumbs.locator('a').click()
        self.wait_dashboard(page)
        self.assertEqual(self.rows(page, 'component'), ['a1', 'c2', 'c1'])
        # or by selecting it
        self.click_row(page, 'component', 'a1')
        page.locator('#budgetplot .dashboard-drilldown').click()
        self.wait_dashboard(page)
        self.assertEqual(self.rows(page, 'component'), ['c2', 'c1'])
        page.locator('#budgetplot .dashboard-reset').click()
        self.wait_dashboard(page)

        # the filters are kept in the url
        self.click_row(page, 'material', 'steel')
        hash = page.evaluate('() => location.hash')
        page = self.open_dashboard(url + hash)
        self.assertIn('1 of 2 source terms', self.info(page))
        self.assertEqual(self.values(page, 'isotope'), ['K40'])

    def test_limit_bins(self):
        page = self.open(self.url('hitefficiency.view', object=self.hiteff))
        self.wait_plot(page, '#spectrumplot')
        traces = page.evaluate("""() => document.querySelector(
            '#spectrumplot .js-plotly-plot').data.map(t => ({
                name: t.name, x: t.x, y: t.y, fill: t.fill, mode: t.mode}))""")
        # s1 = [1, 2, 0, 4, 5, 0]: bins 2 and 5 are limits, only shaded
        self.assertFalse([t for t in traces if 'markers' in t['mode']])
        (line,) = [t for t in traces if t['name'] == 's1']
        # each measured bin is a full step, broken at the limits
        self.assertEqual(line['x'], [0, 1, 1, 2, None, 3, 4, 4, 5, None])
        self.assertEqual(line['y'][:4], [1, 1, 2, 2])
        # the band runs over every bin, up to the limits' 1 sigma limit
        (band,) = [t for t in traces if t['fill'] == 'toself']
        n = len(band['x']) // 2
        self.assertEqual(band['x'][:n], [0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6])
        hist = self.hiteff.spectra['s1'].hist.m
        self.assertAlmostEqual(band['y'][4], hist[2].ppf(ONE_SIGMA_CL))
        self.assertGreater(band['y'][4], 0)
        self.assertLess(band['y'][4], hist[2].ppf(0.9))


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


class TestLocationOverrideEditor(BrowserTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.becu = EmissionSpec(name='BeCu', sources=dict(
            U238='1 mBq/kg')).save()
        self.radon = EmissionSpec(name='radon', sources=dict(
            Pb210='1 mBq/kg')).save()
        HitEfficiency(source='Pb210', location='Package Outer Surface'
                      ).save()
        inner = Component(name='inner', mass='1 kg', location='Inside',
                          specs=[self.becu]).save()
        package = Component(name='package', mass='1 kg', location='Package',
                            specs=[self.becu, self.radon]).save()
        self.a1 = Assembly(name='a1', components=[inner, package]).save()

    def test_placement_filters_specs(self):
        page = self.open(self.url('component.edit', object=self.a1))
        page.click('#locationoverrides button:text-is("Add")')
        row = page.locator('#location_overrides tbody tr').last
        spec = row.locator('select.overridespec')

        def shown():
            return spec.locator('option').evaluate_all(
                "options => options.filter(o => !o.hidden)"
                ".map(o => o.textContent.trim())")
        self.assertEqual(shown(), ['any', 'BeCu (inner)', 'BeCu (package)',
                                   'radon (package)'])
        # locations autocomplete in added rows too
        page.wait_for_function(
            "document.querySelector('#location_overrides tbody tr:last-child"
            " datalist').options.length > 0")
        row.locator('select.overrideplacement').select_option(
            label='package')
        self.assertEqual(shown(), ['any', 'BeCu (package)',
                                   'radon (package)'])
        spec.select_option(label='radon (package)')
        # a spec of another placement is cleared when it's hidden
        row.locator('select.overrideplacement').select_option(label='inner')
        self.assertEqual(shown(), ['any', 'BeCu (inner)'])
        self.assertEqual(spec.input_value(), '')

        row.locator('select.overrideplacement').select_option(
            label='package')
        spec.select_option(label='radon (package)')
        row.locator('input[name="location_overrides.location"]').fill(
            'Package Outer Surface')
        page.click('#mainform button[type=submit] >> nth=0')
        page.wait_for_url(self.base + self.url('component.view',
                                               object=self.a1))
        a1 = Assembly.objects.get(name='a1')
        override = a1.location_overrides[0]
        self.assertEqual(override.placement, a1.children[1].id)
        self.assertEqual(override.spec.name, 'radon')

        # reopening keeps the choices, filtered
        page.goto(self.base + self.url('component.edit', object=self.a1))
        self.assertEqual(spec.locator('option:checked').inner_text().strip(),
                         'radon (package)')
        self.assertEqual(shown(), ['any', 'BeCu (package)',
                                   'radon (package)'])


class TestRadonExposureEditor(BrowserTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')

    def test_add_period(self):
        page = self.open(self.url('emissionspec.edit', type='radonexposure'))
        page.fill('input[name="name"]', 'cleanroom radon')
        page.click('button[data-tableid="periods"]')
        row = page.locator('#periods tbody tr').last
        row.locator('input[name="periods.radonlevel"]').fill('10 Bq/m**3')
        row.locator('input[name="periods.duration"]').fill('3 day')
        page.click('#mainform button[type=submit] >> nth=0')
        page.wait_for_selector('h2:text-is("Radon exposure")')
        spec = EmissionSpec.objects.get(name='cleanroom radon')
        self.assertEqual(spec.periods[0].columnheight, 10 * units.cm)
        self.assertIn('Pb210', page.locator('h2:text-is("Sources") + table')
                      .inner_text())


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
