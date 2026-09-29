""" Importing assays from radiopurity.org through the web interface """
from unittest import mock
import requests
from werkzeug.datastructures import MultiDict
from bgexplorer.application.auth import Role
from bgexplorer.models.assay import Assay
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase
from tests.test_auth import AuthTestCase
from tests.test_radiopurity import fixture

MUMETAL = ['60b4f3ecae890f84b01b2d7f', '60b4f3ecae890f84b01b2bc2',
           '69151107e1d42af8eb08f0d6']


class RadiopurityMixin:
    def setUp(self):
        super().setUp()
        patcher = mock.patch('bgexplorer.models.radiopurity.requests.post')
        self.post = patcher.start()
        self.addCleanup(patcher.stop)
        self.post.return_value.text = fixture('mumetal')

    def search(self, q='mumetal', version='main', **kwargs):
        return self.client.get(self.url('radiopurity.search', version,
                                        q=q, **kwargs))

    def import_(self, ids, q='mumetal', version='main'):
        data = MultiDict([('q', q), ('include_synonyms', 'true')]
                         + [('id', i) for i in ids])
        return self.client.post(self.url('radiopurity.import_selected',
                                         version), data=data)


class TestRadiopurityPages(RadiopurityMixin, AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')

    def test_overview_link(self):
        html = self.html(self.client.get(self.url('emissionspec.overview')))
        self.assertIn('id="importradiopurity"', html)

    def test_form(self):
        response = self.client.get(self.url('radiopurity.search'))
        self.assertEqual(response.status_code, 200)
        self.post.assert_not_called()
        html = self.html(response)
        self.assertIn('id="searchform"', html)
        # synonyms are included by default
        self.assertRegex(html, r'id="include_synonyms"[^>]*checked')
        self.assertNotIn('id="importform"', html)

    def test_search(self):
        html = self.html(self.search())
        data = self.post.call_args.kwargs['data']
        self.assertEqual(data['query_value'], 'mumetal')
        # the box was unchecked
        self.assertNotIn('include_synonyms', data)
        self.assertIn('3 results for <strong>mumetal</strong>', html)
        for recid in MUMETAL:
            self.assertRegex(html, fr'name="id" value="{recid}" checked')
        self.assertIn('<b>Pb-214</b> < 150 mBq/kg', html)
        # a chain name is flagged
        self.assertIn('title="not a recognized isotope name">Top of 238 U'
                      ' Chain</span>', html)
        self.assertNotIn('already imported', html)

    def test_import(self):
        response = self.import_(MUMETAL[:2])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith(
            self.url('emissionspec.overview')))
        self.assertEqual(
            self.post.call_args.kwargs['data']['include_synonyms'], 'true')
        self.assertEqual(sorted(a.radiopurityid for a in Assay.objects),
                         sorted(MUMETAL[:2]))
        assay = Assay.objects.get(radiopurityid=MUMETAL[0])
        self.assertEqual(assay.name, 'Mumetal')
        self.assertEqual(assay.version_tags, ['main'])
        self.assertIn('Th-234', [s.name for s in assay.sources])
        html = self.html(self.client.get(self.url('emissionspec.overview')))
        self.assertIn('Imported 2 assays', html)

        # now they are marked, and not selected
        html = self.html(self.search())
        self.assertIn('3 results for <strong>mumetal</strong>, 2 already'
                      ' imported', html)
        for recid in MUMETAL[:2]:
            self.assertRegex(html, fr'name="id" value="{recid}" >')
        self.assertRegex(html, fr'name="id" value="{MUMETAL[2]}" checked')
        self.assertIn(self.url('emissionspec.view', object=assay), html)

    def test_import_nothing(self):
        response = self.import_([])
        self.assertEqual(response.status_code, 302)
        self.assertIn('q=mumetal', response.location)
        self.post.assert_not_called()
        self.assertEqual(Assay.objects.count(), 0)

    def test_import_missing(self):
        response = self.import_(['notanid', MUMETAL[0]])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Assay.objects.count(), 1)
        html = self.html(self.client.get(response.location))
        self.assertIn('notanid: no longer in the search results', html)

    def test_connection_error(self):
        self.post.side_effect = requests.ConnectionError('no route to host')
        response = self.search()
        self.assertEqual(response.status_code, 200)
        self.assertIn('no route to host', self.html(response))
        response = self.import_(MUMETAL)
        self.assertEqual(response.status_code, 302)
        self.assertIn('no route to host',
                      self.html(self.client.get(response.location)))
        self.assertEqual(Assay.objects.count(), 0)

    def test_format_warning(self):
        self.post.return_value.text = fixture('shapes')
        html = self.html(self.search())
        self.assertIn('Its page format may have changed', html)
        self.assertIn("ranges can't be imported", html)

    def test_readonly(self):
        vc.create_tag('t', 'main')
        self.assertEqual(self.import_(MUMETAL, version='t').status_code, 403)
        self.assertEqual(Assay.objects.count(), 0)


class TestRadiopurityRoles(RadiopurityMixin, AuthTestCase):
    def test_roles(self):
        for role, allowed in [(Role.viewer, False), (Role.editor, True)]:
            with self.subTest(role=role.name):
                self.client = self.app.test_client()
                self.make_user(role.name, role)
                self.login(role.name)
                self.assertEqual(self.search().status_code,
                                 200 if allowed else 403)
                self.assertEqual(self.import_(MUMETAL[:1]).status_code,
                                 302 if allowed else 403)
        self.assertEqual(Assay.objects.get().enteredby, 'editor')

    def test_anonymous(self):
        response = self.search()
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.url('auth.login'), response.location)
        self.post.assert_not_called()
