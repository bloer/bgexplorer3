""" JSON API for versions and their settings """
import unittest
import mongoengine
from bgexplorer.application.app import create_app
from bgexplorer.models.component import Component
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.settings import VersionSettings, get_settings
from bgexplorer.models.versioncontrol import version_exists, create_tag
from tests.dbutil import TEST_MONGODB_URI, connect_test_db
from tests.test_app import reset_database

API = '/api/v1'


class TestAPIVersions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()
        mongoengine.disconnect()

    def setUp(self):
        reset_database()
        self.app = create_app(config={'MONGODB_URI': TEST_MONGODB_URI,
                                      'TESTING': True,
                                      'WTF_CSRF_ENABLED': False})
        self.client = self.app.test_client()
        HitEfficiency(source='K40', location='c1',
                      scalars=dict(v1='0.1 dru/mBq')).save()
        Component(name='c1').save()

    def tearDown(self):
        mongoengine.disconnect()

    def create(self, **body):
        return self.client.post(f'{API}/versions', json=body)

    def assertError(self, response, status, field=None):
        self.assertEqual(response.status_code, status, response.get_json())
        error = response.get_json()['error']
        self.assertTrue(error['message'])
        if field is not None:
            self.assertIn(field, error.get('fields', {}))
        return error

    def test_list(self):
        create_tag('v1', 'main')
        response = self.client.get(f'{API}/versions')
        self.assertEqual(response.status_code, 200)
        versions = {v['version_tag']: v
                    for v in response.get_json()['versions']}
        self.assertEqual(set(versions), {'main', 'v1'})
        self.assertEqual(versions['main']['type'], 'branch')
        self.assertTrue(versions['main']['editable'])
        self.assertEqual(versions['v1']['type'], 'tag')
        self.assertEqual(versions['v1']['url'], f'{API}/versions/v1')

    def test_create(self):
        response = self.create(version_tag='empty', description='nothing')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers['Location'],
                         f'{API}/versions/empty')
        data = response.get_json()
        self.assertEqual(data['type'], 'branch')
        self.assertEqual(data['description'], 'nothing')
        self.assertEqual(Component.select_version('empty').count(), 0)

        response = self.create(version_tag='a/branch', **{'from': 'main'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Component.select_version('a/branch').count(), 1)
        self.assertEqual(response.headers['Location'],
                         f'{API}/versions/a/branch')

        response = self.create(version_tag='v1', type='tag',
                               **{'from': 'a/branch'})
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.get_json()['editable'])
        self.assertFalse(get_settings('v1', create=False).editable)
        self.assertEqual(Component.select_version('v1').count(), 1)

    def test_create_errors(self):
        self.assertError(self.create(version_tag='main'), 409, 'version_tag')
        self.assertError(self.create(version_tag='x', type='tag'), 400,
                         'from')
        self.assertError(self.create(version_tag='x', type='other'), 400,
                         'type')
        self.assertError(self.create(version_tag='x', **{'from': 'nope'}),
                         400, 'from')
        self.assertError(self.create(version_tag='api/x'), 400,
                         'version_tag')
        self.assertError(self.create(), 400, 'version_tag')
        self.assertError(self.create(version_tag='x', editable=False), 400,
                         'editable')
        self.assertFalse(version_exists('x'))
        # not JSON
        response = self.client.post(f'{API}/versions',
                                    data={'version_tag': 'x'})
        self.assertError(response, 415)
        response = self.client.post(f'{API}/versions', data='[1]',
                                    content_type='application/json')
        self.assertError(response, 400)
        self.assertFalse(version_exists('x'))

    def test_get(self):
        response = self.client.get(f'{API}/versions/main')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['version_tag'], 'main')
        self.assertEqual(len(data['addsources']), 2)
        self.assertIn('rois', data['hiteffdbconfig'])
        self.assertNotIn('cache_token', data)
        self.assertNotIn('_id', data)
        self.assertError(self.client.get(f'{API}/versions/nope'), 404)
        self.assertFalse(version_exists('nope'))

    def test_patch(self):
        body = {
            'description': 'new description',
            'addsources': [{'source': 'Th232', 'newsource': 'Ra228',
                            'ratio': 0.5}],
            'hiteffdbconfig': {
                'rois': [{'link_spectrum': 's1', 'start': '1 keV',
                          'stop': '10 keV', 'mode': 'integrate',
                          'display_name': 'roi1'}],
                'display_scalars': {'v1': {'display_unit': 'mdru',
                                           'display_name': 'V1'}},
                'extra_columns': ['material'],
            },
        }
        response = self.client.patch(f'{API}/versions/main', json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()
        self.assertEqual(data['description'], 'new description')
        settings = get_settings('main', create=False)
        self.assertEqual(settings.description, 'new description')
        self.assertEqual(len(settings.addsources), 1)
        self.assertEqual(settings.addsources[0].ratio, 0.5)
        roi = settings.hiteffdbconfig.rois[0]
        self.assertEqual(roi.start.m, 1)
        self.assertEqual(roi.mode.value, 'integrate')
        scalar = settings.hiteffdbconfig.display_scalars['v1']
        self.assertEqual(str(scalar.display_unit), 'mdru')
        self.assertEqual(settings.hiteffdbconfig.extra_columns, ['material'])
        # round trip
        self.assertEqual(data, self.client.get(f'{API}/versions/main')
                         .get_json())
        self.assertEqual(data['hiteffdbconfig']['rois'][0]['start'], '1 keV')
        self.assertEqual(data['hiteffdbconfig']['display_scalars']['v1']
                         ['display_name'], 'V1')

        # a partial update leaves the other fields
        response = self.client.patch(f'{API}/versions/main',
                                     json={'hiteffdbconfig':
                                           {'extra_columns': []}})
        self.assertEqual(response.status_code, 200)
        settings.reload()
        self.assertEqual(settings.hiteffdbconfig.extra_columns, [])
        self.assertEqual(len(settings.hiteffdbconfig.rois), 1)
        self.assertEqual(settings.description, 'new description')

    def test_patch_errors(self):
        def patch(body, tag='main'):
            return self.client.patch(f'{API}/versions/{tag}', json=body)
        before = self.client.get(f'{API}/versions/main').get_json()
        # registered when the hiteff was saved
        self.assertEqual(before['hiteffdbconfig']['display_scalars']['v1']
                         ['display_unit'], 'dru')
        # a display unit incompatible with the hiteffs
        self.assertError(patch({'hiteffdbconfig': {'display_scalars': {
            'v1': {'display_unit': 'kg'}}}}), 400,
            'hiteffdbconfig.display_scalars')
        self.assertError(patch({'hiteffdbconfig': {'display_scalars': {
            'v1': {'display_unit': 'notaunit'}}}}), 400,
            'hiteffdbconfig.display_scalars.display_unit')
        self.assertError(patch({'addsources': [{'source': 'U238',
                                                'bogus': 1}]}), 400,
                         'addsources')
        self.assertError(patch({'addsources': [{'source': 'U238',
                                                'newsource': 'U238'}]}), 400)
        self.assertError(patch({'editable': False}), 400, 'editable')
        self.assertError(patch({'version_tag': 'x'}), 400, 'version_tag')
        self.assertError(patch({'hiteffdbconfig': {'bogus': 1}}), 400,
                         'hiteffdbconfig.bogus')
        self.assertError(patch({}, 'nope'), 404)
        # nothing was changed
        self.assertEqual(self.client.get(f'{API}/versions/main').get_json(),
                         before)

        create_tag('v1', 'main')
        self.assertError(patch({'description': 'x'}, 'v1'), 403)
        self.assertIsNone(get_settings('v1', create=False).description)

    def test_delete(self):
        create_tag('v1', 'main')
        response = self.client.delete(f'{API}/versions/v1')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(version_exists('v1'))
        self.assertEqual(Component.objects.count(), 1)
        self.assertError(self.client.delete(f'{API}/versions/v1'), 404)
        self.assertError(self.client.delete(f'{API}/versions/main'), 403)
        self.assertTrue(version_exists('main'))


if __name__ == '__main__':
    unittest.main()
