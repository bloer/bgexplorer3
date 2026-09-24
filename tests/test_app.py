""" Smoke tests for the web application: every GET endpoint should render
for every object without errors
"""
import os
import unittest
import flask
import mongoengine
from io import BytesIO
from bgexplorer.application.app import create_app
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.verdoc import VersionedDocument
from tests.dbutil import TEST_MONGODB_URI, connect_test_db

# populating the full examples takes a few minutes, so is opt-in
RUN_EXAMPLES = bool(os.environ.get('BGEXPLORER_TEST_EXAMPLES'))

# endpoints that aren't pages
SKIP_ENDPOINTS = {'static', 'test'}

# pages that don't work yet. These are expected to fail so that fixing them
# is noticed.
# TODO: add edit templates for emissionspecs and hiteffs
KNOWN_BROKEN = {'emissionspec.edit', 'hitefficiency.edit'}


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
                                     'TESTING': True})
        cls.populate()

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
                if 'attachmentid' in rule.arguments:
                    for attachment in getattr(obj, 'attachments', []):
                        yield rule.endpoint, rule.build(
                            dict(values, attachmentid=str(attachment.id)))[1]
                    continue
                yield rule.endpoint, rule.build(values)[1]

    def check_get(self, endpoint, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200,
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
                         'emissionspec.sourceterms', 'hitefficiency.view'):
            self.assertIn(endpoint, tested)

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
    @classmethod
    def populate(cls):
        HitEfficiency(source='Th232', location='c1',
                      scalars=dict(v1='0.1 +- 0.01 dru/mBq')).save()
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
            url = flask.url_for('component.add_attachments', object=c1)
        response = client.post(url, data=dict(
            fupload=(BytesIO(b'hello'), 'hello.txt'),
            description='test attachment'))
        assert response.status_code == 302, response.status_code


@unittest.skipUnless(RUN_EXAMPLES, "set BGEXPLORER_TEST_EXAMPLES=1 to run")
class TestAppExamples(AppSmokeTest, unittest.TestCase):
    """ The full examples models """
    version = 'examples/qis'
    maxobjects = 5

    @classmethod
    def populate(cls):
        from bgexplorer.application.examples import qis
        qis.populate_example(version_tag=cls.version, clean=True)
        c1 = Component.select_version(cls.version).first()
        with cls.app.test_request_context():
            flask.g.active_version = cls.version
            url = flask.url_for('component.add_attachments', object=c1)
        cls.app.test_client().post(url, data=dict(
            fupload=(BytesIO(b'hello'), 'hello.txt'), description='test'))


if __name__ == '__main__':
    unittest.main()
