""" Creating, cloning and importing components through the web interface """
import io
import json
import tarfile
import unittest
import flask
import mongoengine
from html import unescape
from bgexplorer.application.app import create_app
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models import versioncontrol as vc
from bgexplorer.models.settings import VersionSettings
from bgexplorer.models.sourceterm import SourceTerm, CalculatedResults
from bgexplorer.models.hiteff import HitEfficiency
from tests.dbutil import TEST_MONGODB_URI
from tests.test_app import reset_database


class AppTestCase(unittest.TestCase):
    """ Base: a fresh app and database for each test class """
    @classmethod
    def setUpClass(cls):
        reset_database()
        cls.app = create_app(config={'MONGODB_URI': TEST_MONGODB_URI,
                                     'TESTING': True,
                                     'WTF_CSRF_ENABLED': False})

    @classmethod
    def tearDownClass(cls):
        mongoengine.disconnect()

    def setUp(self):
        for cls in (VersionSettings, Component, EmissionSpec, SourceTerm,
                    CalculatedResults, HitEfficiency):
            cls.drop_collection()
        self.client = self.app.test_client()

    def url(self, endpoint, version='main', **values):
        with self.app.test_request_context():
            flask.g.active_version = version
            return flask.url_for(endpoint, **values)

    def html(self, response):
        return unescape(response.get_data(as_text=True))


class TestComponentPages(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='K40', rate='1 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='1 kg', specs=[self.e1]).save()
        self.a1 = Assembly(name='a1', children=[
            Placement(component=self.c1, weight=2)]).save()
        vc.create_version('b', 'main')
        vc.create_tag('t', 'main')

    def test_new(self):
        """ new documents are saved in the active version """
        for type_, cls in ((None, Component), ('assembly', Assembly)):
            with self.subTest(type=type_):
                url = self.url('component.edit', 'b', type=type_)
                self.assertEqual(self.client.get(url).status_code, 200)
                response = self.client.post(url, data={
                    'name': f'new {type_}', 'mass': '3 kg'})
                self.assertEqual(response.status_code, 302)
                obj = cls.select_version('b').get(name=f'new {type_}')
                self.assertIs(type(obj), cls)
                self.assertEqual(obj.version_tags, ['b'])
                self.assertIn(self.url('component.view', 'b', object=obj),
                              response.headers['Location'])
        self.assertEqual(Component.select_version('main').count(), 2)
        self.assertEqual(
            self.client.get(self.url('component.edit', 'b', type='nope'))
            .status_code, 400)

    def test_new_errors(self):
        url = self.url('component.edit', 'b')
        response = self.client.post(url, data={'mass': 'heavy'})
        self.assertEqual(response.status_code, 400)
        html = self.html(response)
        self.assertIn('id="toperror"', html)
        self.assertIn('mass', html)
        self.assertEqual(Component.select_version('b').count(), 2)

    def test_edit_redirects(self):
        url = self.url('component.edit', 'b', object=self.c1)
        response = self.client.post(url, data={'description': 'edited'})
        self.assertEqual(response.status_code, 302)
        c1 = Component.select_version('b').get(name='c1')
        self.assertEqual(c1.description, 'edited')
        self.assertEqual(c1.specs[0].name, 'e1')
        self.assertIsNone(Component.select_version('main')
                          .get(name='c1').description)

    def test_clone(self):
        html = self.html(self.client.get(self.url('component.view', 'b',
                                                  object=self.a1)))
        self.assertIn('id="cloneform"', html)
        response = self.client.post(self.url('component.clone', 'b',
                                             object=self.a1))
        self.assertEqual(response.status_code, 302)
        copy = Assembly.select_version('b').get(name='a1 (copy)')
        self.assertIn(self.url('component.edit', 'b', object=copy),
                      response.headers['Location'])
        self.assertEqual(copy.children[0].component.original_id,
                         self.c1.original_id)
        self.assertEqual(Assembly.select_version('main').count(), 1)

    def import_file(self, version, data, filename):
        return self.client.post(
            self.url('component.import_', version),
            data={'file': (io.BytesIO(data), filename)},
            content_type='multipart/form-data', follow_redirects=True)

    def test_import(self):
        exported = self.client.get(self.url('component.get_json',
                                            object=self.a1)).get_data()
        vc.create_version('empty')
        response = self.import_file('empty', exported, 'a1.json')
        self.assertEqual(response.status_code, 200)
        html = self.html(response)
        self.assertIn('Imported 1 documents', html)
        self.assertIn('Dropped references', html)
        self.assertEqual(Assembly.select_version('empty').get().children, [])

        # an archive with both
        docs = [json.loads(self.client.get(self.url(
            'component.get_json', object=obj)).get_data()) for obj in
            (self.a1, self.c1)]
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            data = json.dumps(docs).encode()
            info = tarfile.TarInfo('all.json')
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        vc.create_version('empty2')
        html = self.html(self.import_file('empty2', buf.getvalue(),
                                          'all.tar.gz'))
        self.assertIn('Imported 2 documents', html)
        a1 = Assembly.select_version('empty2').get()
        self.assertEqual(a1.children[0].component.name, 'c1')
        # c1's spec isn't in empty2
        self.assertIn('Dropped references', html)

        response = self.import_file('empty2', b'nonsense', 'x.json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('not valid JSON', self.html(response))

    def test_readonly(self):
        """ tags have no buttons and refuse changes """
        html = self.html(self.client.get(self.url('component.overview',
                                                  't')))
        self.assertNotIn('id="newcomponent"', html)
        self.assertNotIn('id="import"', html)
        html = self.html(self.client.get(self.url('component.view', 't',
                                                  object=self.c1)))
        self.assertNotIn('id="cloneform"', html)
        for endpoint, kwargs in (('component.edit', {}),
                                 ('component.clone', {'object': self.c1}),
                                 ('component.import_', {})):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(self.url(endpoint, 't',
                                                     **kwargs))
                self.assertEqual(response.status_code, 403)
        html = self.html(self.client.get(self.url('component.overview',
                                                  'b')))
        self.assertIn('id="newcomponent"', html)

    def test_other_versions(self):
        c1b = Component.select_version('b').get(name='c1')
        c1b.description = 'edited'
        c1b.save()
        html = self.html(self.client.get(self.url('component.view', 'b',
                                                  object=self.c1)))
        self.assertIn('id="otherversions"', html)
        for tag in ('main', 't'):
            url = self.url('component.view', tag, object=self.c1)
            self.assertIn(f'href="{url}"', html)
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertIn('text-bg-secondary">tag', html)
        for endpoint, obj in (('emissionspec.view', self.e1),):
            html = self.html(self.client.get(self.url(endpoint, 'b',
                                                      object=obj)))
            self.assertIn(self.url(endpoint, 't', object=obj), html)
