""" Pages carry an ETag and are answered with 304 until something changes """
from bgexplorer.models.component import Component
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.settings import get_settings
from bgexplorer.models.users import Role
from bgexplorer.versioning import control as vc
from tests.test_app_components import AppTestCase
from tests.test_auth import AuthTestCase


class TestPageCaching(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='K40', rate='1 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='1 kg', specs=[self.e1]).save()
        self.url_c1 = self.url('component.view', object=self.c1)

    def get(self, url, etag=None, client=None):
        headers = {'If-None-Match': etag} if etag else {}
        return (client or self.client).get(url, headers=headers)

    def test_etag_and_304(self):
        response = self.get(self.url_c1)
        self.assertEqual(response.status_code, 200)
        etag = response.headers['ETag']
        self.assertEqual(response.headers['Cache-Control'],
                         'private, no-cache')
        self.assertNotIn('Last-Modified', response.headers)
        again = self.get(self.url_c1, etag)
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.get_data(), b'')
        self.assertEqual(again.headers['ETag'], etag)

    def test_json(self):
        url = self.url('component.get_json', objid=str(self.c1.original_id))
        etag = self.get(url).headers['ETag']
        self.assertEqual(self.get(url, etag).status_code, 304)

    def test_table_data(self):
        for url in (self.url('component.overview_data'),
                    self.url('component.sourceterm_data', object=self.c1)
                    + '?draw=1&start=0&length=10'):
            with self.subTest(url=url):
                response = self.get(url)
                self.assertEqual(response.status_code, 200)
                etag = response.headers['ETag']
                self.assertEqual(self.get(url, etag).status_code, 304)

    def test_changes(self):
        etag = self.get(self.url_c1).headers['ETag']
        # a change to the component
        response = self.client.post(
            self.url('component.edit', object=self.c1),
            data={'name': 'c1 renamed', 'mass': '2 kg'})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('ETag', response.headers)
        # the page that shows the flash message isn't cached, the next is
        self.assertNotIn('ETag', self.get(self.url_c1, etag).headers)
        response = self.get(self.url_c1, etag)
        self.assertEqual(response.status_code, 200)
        etag = response.headers['ETag']
        self.assertIn('c1 renamed', self.html(response))
        self.assertEqual(self.get(self.url_c1, etag).status_code, 304)
        # a change to anything else in the version, such as a spec
        spec = EmissionSpec.select_version('main').get(name='e1')
        spec.name = 'e1 renamed'
        spec.save()
        response = self.get(self.url_c1, etag)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.headers['ETag'], etag)

    def test_settings_change(self):
        """ the settings are saved with the version, so changing them changes
        the version's pages
        """
        etag = self.get(self.url_c1).headers['ETag']
        self.assertEqual(self.get(self.url_c1, etag).status_code, 304)
        settings = get_settings('main')
        settings.addsources = []
        settings.save()
        response = self.get(self.url_c1, etag)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.headers['ETag'], etag)

    def test_other_version(self):
        vc.create_version('b', 'main')
        etag = self.get(self.url_c1).headers['ETag']
        compare = self.url('compare_versions') + '?with=b'
        compare_etag = self.get(compare).headers['ETag']
        spec = EmissionSpec.select_version('b').get(name='e1')
        spec.name = 'changed in b'
        spec.save()
        self.assertEqual(self.get(self.url_c1, etag).status_code, 304)
        self.assertEqual(self.get(compare, compare_etag).status_code, 200)

    def test_flash(self):
        etag = self.get(self.url_c1).headers['ETag']
        with self.client.session_transaction() as session:
            session['_flashes'] = [('message', 'saved it')]
        response = self.get(self.url_c1, etag)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('ETag', response.headers)
        self.assertIn('saved it', self.html(response))
        self.assertEqual(self.get(self.url_c1, etag).status_code, 304)

    def test_not_cached(self):
        for endpoint in ('index', 'auth.login'):
            with self.subTest(endpoint=endpoint):
                self.assertNotIn('ETag', self.get(self.url(endpoint)).headers)
        self.assertEqual(
            self.get(self.url('component.view', 'nope', object=self.c1))
            .status_code, 404)

    def test_static(self):
        url = self.url('static', filename='base.css')
        self.assertRegex(url, r'\?v=\d+$')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.cache_control.max_age, 31536000)
        self.assertEqual(self.client.get(
            '/favicon.ico').cache_control.max_age, 86400)


class TestCachingPerUser(AuthTestCase):
    def test_user(self):
        self.make_user('v1', Role.viewer)
        self.make_user('e1', Role.editor)
        c1 = Component(name='c1', mass='1 kg').save()
        url = self.url('component.view', object=c1)
        etags = {}
        for name in ('v1', 'e1'):
            client = self.app.test_client()
            self.assertEqual(self.login(name, client=client).status_code, 302)
            client.get(url)  # shows the login's flash message
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            etags[name] = response.headers['ETag']
            self.assertEqual(
                client.get(url, headers={'If-None-Match': etags[name]})
                .status_code, 304)
        self.assertNotEqual(etags['v1'], etags['e1'])
        # one user's etag doesn't validate another's page
        client = self.app.test_client()
        self.login('v1', client=client)
        self.assertEqual(
            client.get(url, headers={'If-None-Match': etags['e1']})
            .status_code, 200)
