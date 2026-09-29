""" Logging in, and what each role can do """
import flask
from bgexplorer.application.app import create_app
from bgexplorer.models.settings import (ApplicationSettings,
                                        get_application_settings)
from bgexplorer.models.users import User, Role
from bgexplorer.models import versioncontrol as vc
from tests.dbutil import TEST_MONGODB_URI
from tests.test_app_components import AppTestCase

PASSWORD = 'correct horse'


class AuthTestCase(AppTestCase):
    """ An app with logins enabled """
    LOGIN_DISABLED = False

    def setUp(self):
        super().setUp()
        User.drop_collection()
        ApplicationSettings.drop_collection()
        vc.create_version('main')

    def make_user(self, name, role=Role.viewer, **kwargs):
        user = User(name=name, role=role, **kwargs)
        user.set_password(PASSWORD)
        return user.save()

    def login(self, name, password=PASSWORD, client=None, next=None):
        client = client or self.client
        url = self.url('auth.login', **({'next': next} if next else {}))
        return client.post(url, data=dict(username=name, password=password))

    def set_anon_view(self, allow):
        settings = get_application_settings()
        settings.allow_anon_view = allow
        settings.save()


class TestLogin(AuthTestCase):
    def test_login_logout(self):
        self.make_user('u1', Role.editor)
        html = self.html(self.client.get(self.url('index')))
        self.assertIn('id="loginlink"', html)
        self.assertNotIn('id="adminlink"', html)
        for name, password in (('u1', 'wrong password'), ('nobody', PASSWORD)):
            with self.subTest(name=name):
                response = self.login(name, password)
                self.assertEqual(response.status_code, 401)
                self.assertIn('Unknown user name or wrong password',
                              self.html(response))
        response = self.login('u1')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/'))
        html = self.html(self.client.get(self.url('index')))
        self.assertIn('id="profilelink"', html)
        self.assertIn('>u1</a>', html)
        self.assertNotIn('id="loginlink"', html)
        # logging out needs a POST, so other sites can't link to it
        self.assertEqual(self.client.get(self.url('auth.logout')).status_code,
                         405)
        self.assertEqual(self.client.post(self.url('auth.logout'))
                         .status_code, 302)
        self.assertIn('id="loginlink"',
                      self.html(self.client.get(self.url('index'))))

    def test_next(self):
        self.make_user('u1')
        overview = self.url('overview')
        for target, expected in ((overview, overview),
                                 ('https://evil.example/x', '/'),
                                 ('//evil.example/x', '/'),
                                 ('/\\evil.example', '/')):
            with self.subTest(next=target):
                response = self.login('u1', next=target)
                self.assertEqual(response.status_code, 302)
                location = response.headers['Location']
                self.assertNotIn('evil', location)
                self.assertTrue(location.endswith(expected), location)

    def test_anon_view(self):
        self.assertEqual(self.client.get(self.url('overview')).status_code,
                         200)
        self.set_anon_view(False)
        response = self.client.get(self.url('overview'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.url('auth.login'), response.headers['Location'])
        self.assertIn('next=', response.headers['Location'])
        response = self.client.get(self.url('api.list_versions'))
        self.assertEqual(response.status_code, 401)
        self.assertIn('error', response.get_json())
        # these are still public
        self.assertEqual(self.client.get(self.url('auth.login')).status_code,
                         200)
        self.assertEqual(self.client.get(self.url('admin.logo')).status_code,
                         404)
        # any user can view
        self.make_user('u1')
        self.login('u1')
        self.assertEqual(self.client.get(self.url('overview')).status_code,
                         200)
        self.assertEqual(self.client.get(self.url('api.list_versions'))
                         .status_code, 200)

    def test_inactive(self):
        self.set_anon_view(False)
        self.make_user('u1', active=False)
        self.assertEqual(self.login('u1').status_code, 401)
        user = self.make_user('u2')
        self.login('u2')
        self.assertEqual(self.client.get(self.url('index')).status_code, 200)
        user.active = False
        user.save()
        # the existing session no longer works
        self.assertEqual(self.client.get(self.url('index')).status_code, 302)

    def test_profile(self):
        url = self.url('auth.profile')
        self.assertEqual(self.client.get(url).status_code, 302)
        self.make_user('u1', Role.editor)
        self.login('u1')
        other = self.app.test_client()
        self.login('u1', client=other)
        html = self.html(self.client.get(url))
        self.assertIn('id="passwordform"', html)
        self.assertIn('editor', html)
        new = 'battery staple'
        for form, error in (
                (dict(current_password='wrong', new_password=new,
                      confirm_password=new), 'Wrong password'),
                (dict(current_password=PASSWORD, new_password=new,
                      confirm_password='other'), "don't match"),
                (dict(current_password=PASSWORD, new_password='short',
                      confirm_password='short'), 'at least 8')):
            with self.subTest(error=error):
                response = self.client.post(url, data=form)
                self.assertEqual(response.status_code, 400)
                self.assertIn(error, self.html(response))
        self.assertTrue(User.objects.get().check_password(PASSWORD))
        response = self.client.post(url, data=dict(
            current_password=PASSWORD, new_password=new,
            confirm_password=new))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.get().check_password(new))
        # this session continues, others end
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(other.get(url).status_code, 302)
        self.assertEqual(self.login('u1', new, client=other).status_code, 302)

    def test_secret_key(self):
        with self.assertRaisesRegex(RuntimeError, 'SECRET_KEY'):
            create_app(config={'TESTING': False, 'DEBUG': False,
                               'SECRET_KEY': None})
        app = create_app(config={'TESTING': True, 'SECRET_KEY': None,
                                 'MONGODB_URI': TEST_MONGODB_URI})
        self.assertTrue(app.config['SECRET_KEY'])
