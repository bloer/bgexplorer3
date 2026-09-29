""" Deleting documents from a version through the web interface """
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.sourceterm import SourceTerm, CalculatedResults
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase


class TestDelete(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.h1 = HitEfficiency(source='Th232', location='c1',
                                scalars=dict(v1='0.1 +- 0.01 dru/mBq')).save()
        self.h2 = HitEfficiency(source='K40', location='c2',
                                scalars=dict(v1='0.3 +- 0.03 dru/mBq')).save()
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='Th232', rate='10 +- 1 mBq/kg'),
            EmissionSource(name='K40', rate='20 +- 2 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='2 kg', location='c1',
                            specs=[self.e1]).save()
        self.c2 = Component(name='c2', mass='1 kg', location='c2',
                            specs=[self.e1]).save()
        self.a1 = Assembly(name='a1', children=[
            Placement(component=self.c1, weight=2),
            Placement(component=self.c2)]).save()
        vc.create_version('b', 'main')
        vc.create_tag('t', 'main')
        # fill the result caches, which deleting must not leave stale
        self.before = {name: self.total(name, 'b')
                       for name in ('a1', 'c1', 'c2')}
        self.assertEqual(self.before['c1'], '2.0+/-0.3 dru')

    def total(self, name, version='b'):
        """ The v1 result of component `name`, or None """
        obj = Component.select_version(version).get(name=name)
        results = CalculatedResults.for_object(obj, spectra=False)
        if results is None or 'v1' not in results.scalars:
            return None
        return str(results.scalars['v1'])

    def delete(self, obj, version='b'):
        url = self.url(f'{obj._class_name.split(".")[0].lower()}.delete',
                       version, object=obj)
        page = self.html(self.client.get(url))
        response = self.client.post(url)
        return page, response

    def check_main(self):
        """ the other versions are unchanged """
        for version in ('main', 't'):
            self.assertEqual(EmissionSpec.select_version(version).count(), 1)
            self.assertEqual(Component.select_version(version).count(), 3)
            self.assertEqual(HitEfficiency.select_version(version).count(), 2)
            for name, total in self.before.items():
                self.assertEqual(self.total(name, version), total)

    def check_livetimes(self, version='b'):
        for st in SourceTerm.select_version(version):
            self.assertEqual(len(st.livetimes), len(st.hiteffs))

    def test_delete_spec(self):
        page, response = self.delete(self.e1)
        self.assertIn('id="deleteform"', page)
        self.assertIn('>c1</a>', page)
        self.assertIn('>c2</a>', page)
        self.assertIn('id="otherversions"', page)
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.url('emissionspec.overview', 'b'),
                      response.headers['Location'])
        self.assertEqual(EmissionSpec.select_version('b').count(), 0)
        c1 = Component.select_version('b').get(name='c1')
        self.assertEqual(list(c1.specs), [])
        self.assertEqual(SourceTerm.select_version('b').count(), 0)
        for name in ('a1', 'c1', 'c2'):
            self.assertIsNone(self.total(name))
        self.check_main()

    def test_delete_component(self):
        page, response = self.delete(self.c1)
        self.assertIn('>a1</a>', page)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Component.select_version('b').count(), 2)
        a1 = Assembly.select_version('b').get(name='a1')
        self.assertEqual([p.component.name for p in a1.children], ['c2'])
        self.assertEqual(self.total('a1'), self.before['c2'])
        self.check_livetimes()
        self.check_main()

    def test_delete_assembly(self):
        page, response = self.delete(self.a1)
        self.assertIn('children are kept', page)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(sorted(c.name for c in
                                Component.select_version('b')), ['c1', 'c2'])
        self.assertEqual(self.total('c1'), self.before['c1'])
        self.check_main()

    def test_delete_hiteff(self):
        page, response = self.delete(self.h1)
        self.assertIn('2 source terms', page)
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.url('hitefficiency.overview', 'b'),
                      response.headers['Location'])
        self.assertEqual(HitEfficiency.select_version('b').count(), 1)
        self.check_livetimes()
        # c1's only hit efficiency is gone
        self.assertIsNone(self.total('c1'))
        self.assertEqual(self.total('c2'), self.before['c2'])
        self.assertEqual(self.total('a1'), self.before['c2'])
        self.check_main()

    def test_links(self):
        for obj in (self.e1, self.c1, self.h1):
            endpoint = obj._class_name.split('.')[0].lower()
            with self.subTest(endpoint=endpoint):
                html = self.html(self.client.get(
                    self.url(f'{endpoint}.view', 'b', object=obj)))
                self.assertIn('id="deletelink"', html)
                html = self.html(self.client.get(
                    self.url(f'{endpoint}.view', 't', object=obj)))
                self.assertNotIn('id="deletelink"', html)

    def test_readonly(self):
        url = self.url('component.delete', 't', object=self.c1)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.check_main()

    def test_missing(self):
        url = self.url('component.delete', 'b', object=self.c1)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url).status_code, 404)
