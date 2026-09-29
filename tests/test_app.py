""" Smoke tests for the web application: every GET endpoint should render
for every object without errors
"""
import os
import unittest
import flask
import mongoengine
from io import BytesIO
from html import unescape
from bgexplorer.application.app import create_app
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.verdoc import VersionedDocument
from bgexplorer.models.settings import get_settings
from bgexplorer.models import versioncontrol as vc
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.users import User
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.common import units
from tests.dbutil import TEST_MONGODB_URI, connect_test_db
import numpy as np

# populating the full examples takes a few minutes, so is opt-in
RUN_EXAMPLES = bool(os.environ.get('BGEXPLORER_TEST_EXAMPLES'))

# endpoints that aren't pages
SKIP_ENDPOINTS = {'static', 'test'}

# pages that don't work yet. These are expected to fail so that fixing them
# is noticed.
KNOWN_BROKEN = set()
# endpoints whose GET doesn't return 200 with the test fixtures
EXPECTED_STATUS = {'admin.logo': 404}  # no logo is set


def reset_database():
    connect_test_db()
    db = mongoengine.get_db()
    db.client.drop_database(db.name)
    mongoengine.disconnect()


class AppSmokeTest:
    """ Mixin: subclasses must set `version` and populate the database in
    `populate`
    """
    version = VersionedDocument.get_default_tag()
    # max objects per collection to test
    maxobjects = None

    @classmethod
    def populate(cls):
        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        reset_database()
        cls.app = create_app(config={'MONGODB_URI': TEST_MONGODB_URI,
                                     'TESTING': True, 'LOGIN_DISABLED': True,
                                     'WTF_CSRF_ENABLED': False})
        cls.populate()
        # for the user admin pages
        User(name='smoketest').save()

    @classmethod
    def tearDownClass(cls):
        mongoengine.disconnect()

    def setUp(self):
        self.client = self.app.test_client()

    def objects(self, blueprint):
        cls = self.app.blueprints[blueprint].doc_cls
        return list(cls.select_version(self.version)[:self.maxobjects])

    def urls(self):
        """ Yield (endpoint, url) for every GET endpoint and object """
        for rule in self.app.url_map.iter_rules():
            if ('GET' not in rule.methods or rule.endpoint in SKIP_ENDPOINTS
                    or rule.endpoint.endswith('.static')):
                continue
            values = {}
            if 'active_version' in rule.arguments:
                values['active_version'] = self.version
            if 'objid' not in rule.arguments:
                objects = [None]
            else:
                blueprint = rule.endpoint.split('.')[0]
                objects = self.objects(blueprint)
                self.assertTrue(objects, f"No objects to test {rule}")
            for obj in objects:
                if obj is not None:
                    values['objid'] = str(obj.original_id)
                if 'userid' in rule.arguments:
                    for user in User.objects:
                        yield rule.endpoint, rule.build(
                            dict(values, userid=str(user.id)))[1]
                    continue
                if 'attachmentid' in rule.arguments:
                    for attachment in getattr(obj, 'attachments', []):
                        yield rule.endpoint, rule.build(
                            dict(values, attachmentid=str(attachment.id)))[1]
                    continue
                yield rule.endpoint, rule.build(values)[1]

    def check_get(self, endpoint, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code,
                         EXPECTED_STATUS.get(endpoint, 200),
                         f"{endpoint}: GET {url}")
        return response

    def test1_get_all(self):
        tested = set()
        for endpoint, url in self.urls():
            if endpoint in KNOWN_BROKEN:
                continue
            tested.add(endpoint)
            with self.subTest(url=url):
                self.check_get(endpoint, url)
        # make sure the url discovery found everything
        for endpoint in ('index', 'overview', 'component.view',
                         'component.get_attachment',
                         'emissionspec.get_attachment',
                         'emissionspec.sourceterms', 'hitefficiency.view',
                         'versions.new', 'versions.delete',
                         'edit_settings', 'api.list_versions', 'api.get_version'):
            self.assertIn(endpoint, tested)

    @unittest.skipUnless(KNOWN_BROKEN, "no known broken pages")
    @unittest.expectedFailure
    def test2_known_broken(self):
        for endpoint, url in self.urls():
            if endpoint in KNOWN_BROKEN:
                self.check_get(endpoint, url)

    def test3_relativeto(self):
        """ component views relative to an assembly """
        for assembly in Assembly.select_version(self.version)[:3]:
            for placement in assembly.children:
                with self.app.test_request_context():
                    flask.g.active_version = self.version
                    url = flask.url_for('component.view',
                                        object=placement.component,
                                        relativeto=assembly)
                self.check_get('component.view', url)


class TestAppSmall(AppSmokeTest, unittest.TestCase):
    """ A small model covering every collection """
    def loaded(self, endpoint, obj, field):
        """ Is `field` loaded for `obj` at `endpoint`? """
        blueprint = self.app.blueprints[endpoint.split('.')[0]]
        with self.app.test_request_context():
            flask.g.active_version = self.version
            url = flask.url_for(endpoint, object=obj)
        with self.app.test_request_context(url):
            self.app.preprocess_request()
            return flask.g.object.is_loaded(field)

    def test4_deferred_fields(self):
        """ Large fields are only loaded by endpoints that need them """
        hiteff = HitEfficiency.select_version(self.version).get(source='Th232')
        c1 = Component.select_version(self.version).get(name='c1')
        self.assertFalse(self.loaded('hitefficiency.view', hiteff, 'spectra'))
        self.assertTrue(self.loaded('hitefficiency.get_json', hiteff,
                                    'spectra'))
        self.assertFalse(self.loaded('component.view', c1, 'attachments'))
        self.assertTrue(self.loaded('component.get_json', c1, 'attachments'))
        with self.app.test_request_context():
            flask.g.active_version = self.version
            url = flask.url_for('hitefficiency.get_json', object=hiteff)
        self.assertIn('s1', self.client.get(url).get_json()['spectra'])

    @classmethod
    def populate(cls):
        HitEfficiency(source='Th232', location='c1',
                      scalars=dict(v1='0.1 +- 0.01 dru/mBq'),
                      spectra=dict(s1=Histogram(
                          AsymmetricUncertainty.fromcounts(np.arange(5.))
                          * units('dru/mBq'), np.arange(6.) * units.keV)),
                      ).save()
        HitEfficiency(source='K40', location='a1',
                      scalars=dict(v1='<0.2 dru/mBq')).save()
        e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='Th232', rate='10 +- 1 mBq/kg'),
            EmissionSource(name='K40', rate='<25 mBq/kg'),
            EmissionSource(name='U238', rate='1 ppb'),
            ]).save()
        c1 = Component(name='c1', mass='2 kg', location='c1', specs=[e1],
                       sources=[EmissionSource(name='Co60',
                                               rate='2 mBq/kg')]).save()
        c2 = Component(name='c2', surface_area='1 m**2', specs=[e1]).save()
        a1 = Assembly(name='a1', location='a1', children=[
            Placement(component=c1, weight=2),
            Placement(component=c2, label='c2 label'),
            ]).save()
        Assembly(name='a2', components=[a1, c1]).save()

        # add an attachment through the web interface
        client = cls.app.test_client()
        with cls.app.test_request_context():
            flask.g.active_version = cls.version
            urls = [flask.url_for('component.add_attachments', object=c1),
                    flask.url_for('emissionspec.add_attachments', object=e1)]
        for url in urls:
            response = client.post(url, data=dict(
                fupload=(BytesIO(b'hello'), 'hello.txt'),
                description='test attachment'))
            assert response.status_code == 302, response.status_code


class TestAppVersions(unittest.TestCase):
    """ Pages to manage versions """
    @classmethod
    def setUpClass(cls):
        reset_database()
        cls.app = create_app(config={'MONGODB_URI': TEST_MONGODB_URI,
                                     'TESTING': True, 'LOGIN_DISABLED': True,
                                     'WTF_CSRF_ENABLED': False})

    @classmethod
    def tearDownClass(cls):
        mongoengine.disconnect()

    def setUp(self):
        for version in vc.list_versions():
            if version.version_tag != 'main':
                vc.delete_version(version.version_tag)
        Component.drop_collection()
        self.c1 = Component(name='c1').save()
        vc.create_tag('v1', 'main', description='first tag')
        self.client = self.app.test_client()

    def url(self, endpoint, **values):
        with self.app.test_request_context():
            return flask.url_for(endpoint, **values)

    def test_index(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('first tag', html)
        self.assertIn(self.url('overview', active_version='v1'), html)
        self.assertIn(self.url('overview', active_version='main'), html)
        vc.create_branch('b1', 'main')
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn(self.url('versions.delete', active_version='b1'), html)
        # neither tags nor the default version can be deleted
        for tag in ('v1', 'main'):
            self.assertNotIn(self.url('versions.delete', active_version=tag),
                             html)
            html2 = self.client.get(self.url('overview', active_version=tag))
            self.assertNotIn(self.url('versions.delete', active_version=tag),
                             html2.get_data(as_text=True))

    def test_new(self):
        url = self.url('versions.new')
        response = self.client.get(url, query_string={'from': 'v1',
                                                      'type': 'tag'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('<option value="v1" selected>',
                      response.get_data(as_text=True))
        response = self.client.post(url, data={'version_tag': 'b1',
                                               'from': 'v1', 'type': 'branch',
                                               'description': 'a branch'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location,
                         self.url('overview', active_version='b1'))
        settings = get_settings('b1', create=False)
        self.assertTrue(settings.editable)
        self.assertEqual(settings.description, 'a branch')
        self.assertEqual(Component.select_version('b1').count(), 1)
        # the flash message is shown
        html = self.client.get(response.location).get_data(as_text=True)
        self.assertIn("Created branch 'b1' from 'v1'", unescape(html))

        response = self.client.post(url, data={'version_tag': 'b1'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('already exists', response.get_data(as_text=True))
        response = self.client.post(url, data={'version_tag': 't2',
                                               'type': 'tag'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(vc.version_exists('t2'))

    def test_delete(self):
        vc.create_branch('b1', 'main')
        url = self.url('versions.delete', active_version='b1')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="deleteversion"', response.get_data(as_text=True))
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(vc.version_exists('b1'))
        self.assertEqual(Component.select_version('main').count(), 1)
        self.assertEqual(self.client.get(url).status_code, 404)

        url = self.url('versions.delete', active_version='v1')
        html = self.client.get(url).get_data(as_text=True)
        self.assertNotIn('id="deleteversion"', html)
        self.assertIn('Tags are permanent', unescape(html))
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertTrue(vc.version_exists('v1'))

        url = self.url('versions.delete', active_version='main')
        html = self.client.get(url).get_data(as_text=True)
        self.assertNotIn('id="deleteversion"', html)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertTrue(vc.version_exists('main'))

    def test_route_names_as_versions(self):
        """ versions named like top-level routes don't collide with them """
        for tag in ('api', 'versions', 'component'):
            vc.create_version(tag, 'main')
            url = self.url('component.view', active_version=tag,
                           objid=str(self.c1.original_id))
            self.assertEqual(url, f'/explore/{tag}/component/'
                                  f'{self.c1.original_id}')
            self.assertEqual(self.client.get(url).status_code, 200)
            self.assertEqual(self.client.get(f'/explore/{tag}').status_code,
                             200)
        self.assertEqual(self.client.get('/api/v1/versions').status_code, 200)

    def test_unknown_version(self):
        response = self.client.get('/explore/nope')
        self.assertEqual(response.status_code, 404)
        self.assertIn("Version 'nope' does not exist",
                      unescape(response.get_data(as_text=True)))
        self.assertEqual(self.client.get('/explore/nope/component/').status_code,
                         404)
        self.assertFalse(vc.version_exists('nope'))
        response = self.client.get('/api/v1/nothing')
        self.assertEqual(response.status_code, 404)
        self.assertIn('error', response.get_json())

    def test_readonly(self):
        c1 = Component.select_version('v1').get()
        edit = self.url('component.edit', active_version='v1', object=c1)
        view = self.url('component.view', active_version='v1', object=c1)
        html = self.client.get(view).get_data(as_text=True)
        self.assertIn('tag: read-only', html)
        self.assertNotIn(edit, html)
        self.assertEqual(self.client.get(edit).status_code, 403)
        response = self.client.post(edit, data={'name': 'changed'})
        self.assertEqual(response.status_code, 403)
        self.assertIn('read-only', response.get_data(as_text=True))
        attach = self.url('component.add_attachments', active_version='v1',
                          object=c1)
        response = self.client.post(attach, data=dict(
            fupload=(BytesIO(b'hello'), 'hello.txt'), description='x'))
        self.assertEqual(response.status_code, 403)
        c1.reload()
        self.assertEqual(c1.name, 'c1')
        self.assertEqual(len(c1.attachments), 0)
        # the branch can be edited
        edit = self.url('component.edit', active_version='main',
                        object=self.c1)
        self.assertIn(edit, self.client.get(self.url(
            'component.view', active_version='main', object=self.c1))
            .get_data(as_text=True))
        self.assertEqual(self.client.get(edit).status_code, 200)

    def test_settings(self):
        HitEfficiency(source='K40', location='c1',
                      scalars=dict(v1='0.1 dru/mBq')).save()
        url = self.url('edit_settings', active_version='main')
        html = self.client.get(self.url('overview', active_version='main'))\
            .get_data(as_text=True)
        self.assertIn(url, html)
        html = self.client.get(url).get_data(as_text=True)
        self.assertIn('name="hiteffdbconfig.display_scalars[v1].display_unit"',
                      html)
        self.assertIn('name="addsources.source"', html)
        scalar = 'hiteffdbconfig.display_scalars[v1]'
        data = {
            'description': 'edited',
            '_listfields': ['addsources', 'hiteffdbconfig.rois',
                            'hiteffdbconfig.extra_columns', 'editable'],
            'addsources.source': ['Th232'],
            'addsources.newsource': ['Ra228'],
            'addsources.ratio': ['0.5'],
            'addsources.ratiotype': ['abundance'],
            'addsources.comment': [''],
            'hiteffdbconfig.extra_columns': ['material', ''],
            f'{scalar}.display_name': 'V1',
            f'{scalar}.display_unit': 'mdru',
            f'{scalar}.description': '',
            f'{scalar}.link_spectrum': '',
            f'{scalar}.hide': 'true',
            'hiteffdbconfig.rois.display_name': ['roi1', 'roi2'],
            'hiteffdbconfig.rois.link_spectrum': ['s1', 's2'],
            'hiteffdbconfig.rois.start': ['1 keV', '2 keV'],
            'hiteffdbconfig.rois.stop': ['10 keV', '20 keV'],
            'hiteffdbconfig.rois.mode': ['integrate', 'average'],
            'hiteffdbconfig.rois.binwidths': ['false', 'true'],
            'hiteffdbconfig.rois.hide': ['true', 'false'],
            # not editable here
            'version_tag': 'hacked',
            'editable': 'false',
        }
        response = self.client.post(url, data=data)
        self.assertEqual(response.status_code, 302,
                         response.get_data(as_text=True))
        self.assertEqual(response.location,
                         self.url('overview', active_version='main'))
        settings = get_settings('main', create=False)
        self.assertEqual(settings.description, 'edited')
        self.assertTrue(settings.editable)
        self.assertEqual(len(settings.addsources), 1)
        self.assertEqual(settings.addsources[0].ratio, 0.5)
        self.assertEqual(settings.addsources[0].ratiotype.value, 'abundance')
        config = settings.hiteffdbconfig
        self.assertEqual(config.extra_columns, ['material'])
        self.assertEqual(str(config.display_scalars['v1'].display_unit),
                         'mdru')
        self.assertEqual(config.display_scalars['v1'].display_name, 'V1')
        self.assertTrue(config.display_scalars['v1'].hide)
        self.assertEqual([r.display_name for r in config.rois],
                         ['roi1', 'roi2'])
        self.assertEqual(config.rois[0].mode.value, 'integrate')
        self.assertEqual(config.rois[1].start.m, 2)
        self.assertEqual([r.binwidths for r in config.rois], [False, True])
        self.assertEqual([r.hide for r in config.rois], [True, False])
        # the form shows the saved values
        html = self.client.get(url).get_data(as_text=True)
        self.assertIn('value="2 keV"', html)
        self.assertIn('<option value="integrate" selected>', html)

        # errors are shown and nothing is saved
        response = self.client.post(url, data={f'{scalar}.display_unit': 'kg',
                                               'description': 'x'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('hiteffdbconfig.display_scalars',
                      response.get_data(as_text=True))
        response = self.client.post(url, data={
            f'{scalar}.display_unit': 'notaunit',
            'hiteffdbconfig.rois.display_name': ['roi1'],
            'hiteffdbconfig.rois.start': ['1 keV'],
            'hiteffdbconfig.rois.stop': ['bogus'],
            'addsources.source': ['U238'],
            'addsources.newsource': ['U238']})
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertIn('hiteffdbconfig.display_scalars.v1.display_unit', html)
        self.assertIn('hiteffdbconfig.rois.0.stop', html)
        self.assertIn('is-invalid', html)
        response = self.client.post(url, data={
            'addsources.source': ['U238'], 'addsources.newsource': ['U238']})
        self.assertEqual(response.status_code, 400)
        html = unescape(response.get_data(as_text=True))
        self.assertIn('addsources.0.newsource', html)
        self.assertIn('must be different', html)
        settings.reload()
        self.assertEqual(settings.description, 'edited')
        self.assertEqual(str(config.display_scalars['v1'].display_unit),
                         'mdru')

        # lists can be emptied
        response = self.client.post(url, data={
            '_listfields': ['addsources', 'hiteffdbconfig.rois']})
        self.assertEqual(response.status_code, 302)
        settings.reload()
        self.assertEqual(len(settings.addsources), 0)
        self.assertEqual(len(settings.hiteffdbconfig.rois), 0)
        self.assertEqual(settings.hiteffdbconfig.extra_columns, ['material'])

        # tags can't be edited
        tagurl = self.url('edit_settings', active_version='v1')
        self.assertNotIn(tagurl, self.client.get(self.url(
            'overview', active_version='v1')).get_data(as_text=True))
        self.assertEqual(self.client.get(tagurl).status_code, 403)
        self.assertEqual(self.client.post(tagurl, data={'description': 'x'})
                         .status_code, 403)
        self.assertEqual(get_settings('v1', create=False).description,
                         'first tag')


class TestCSRF(unittest.TestCase):
    """ Forms need a CSRF token; the JSON API doesn't """
    @classmethod
    def setUpClass(cls):
        reset_database()
        cls.app = create_app(config={'MONGODB_URI': TEST_MONGODB_URI,
                                     'TESTING': True, 'LOGIN_DISABLED': True})

    @classmethod
    def tearDownClass(cls):
        mongoengine.disconnect()

    def test_csrf(self):
        client = self.app.test_client()
        response = client.post('/versions/new', data={'version_tag': 'b1'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(vc.version_exists('b1'))
        # with the token from the form
        html = client.get('/versions/new').get_data(as_text=True)
        token = html.split('name="csrf_token" value="')[1].split('"')[0]
        response = client.post('/versions/new', data={'version_tag': 'b1',
                                                      'csrf_token': token})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(vc.version_exists('b1'))
        response = client.post('/api/v1/versions',
                               json={'version_tag': 'b2'})
        self.assertEqual(response.status_code, 201)


@unittest.skipUnless(RUN_EXAMPLES, "set BGEXPLORER_TEST_EXAMPLES=1 to run")
class TestAppExamples(AppSmokeTest, unittest.TestCase):
    """ The full examples models """
    version = 'examples-qis'
    maxobjects = 5

    @classmethod
    def populate(cls):
        from bgexplorer.application.examples import qis
        qis.populate_example(version_tag=cls.version, clean=True)
        c1 = Component.select_version(cls.version).first()
        e1 = EmissionSpec.select_version(cls.version).first()
        with cls.app.test_request_context():
            flask.g.active_version = cls.version
            urls = [flask.url_for('component.add_attachments', object=c1),
                    flask.url_for('emissionspec.add_attachments', object=e1)]
        for url in urls:
            cls.app.test_client().post(url, data=dict(
                fupload=(BytesIO(b'hello'), 'hello.txt'), description='test'))


if __name__ == '__main__':
    unittest.main()
