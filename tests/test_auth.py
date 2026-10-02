""" Logging in, and what each role can do """
import flask
from bgexplorer.application.app import create_app
from bgexplorer.models.settings import (ApplicationSettings, get_secret_key,
                                        get_application_settings)
from bgexplorer.models.users import User, Role
from bgexplorer.models import versioncontrol as vc
from bgexplorer.models.component import Component
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
        """ Without a configured key, every app shares a stored one """
        config = {'TESTING': False, 'DEBUG': False, 'SECRET_KEY': None,
                  'MONGODB_URI': TEST_MONGODB_URI}
        key = create_app(config=config).config['SECRET_KEY']
        self.assertTrue(key)
        self.assertEqual(create_app(config=config).config['SECRET_KEY'], key)
        self.assertEqual(get_secret_key(), key)
        configured = create_app(config=dict(config, SECRET_KEY='abc'))
        self.assertEqual(configured.config['SECRET_KEY'], 'abc')


class TestRoles(AuthTestCase):
    """ What each role can do. Anonymous users can view, the default """
    ROLES = [None] + list(Role)

    def setUp(self):
        super().setUp()
        self.c1 = Component(name='c1').save()
        vc.create_tag('t', 'main')
        self.clients = {None: self.client}
        for role in Role:
            self.make_user(role.name, role)
            self.clients[role] = self.app.test_client()
            self.login(role.name, client=self.clients[role])

    def request(self, role, method, url, **kwargs):
        return self.clients[role].open(url, method=method, **kwargs)

    def check(self, needed, method, url, make=None, **kwargs):
        """ Only `needed` and above may request `url`, which may be a
        function of a name unique to the role. `make(name)` is called first,
        and returns the request arguments
        """
        for role in self.ROLES:
            name = role.name if role else 'anonymous'
            with self.subTest(method=method, url=url, role=name):
                args = make(name) if make else {}
                target = url(name) if callable(url) else url
                response = self.request(role, method, target, **args,
                                        **kwargs)
                status = response.status_code
                # anonymous users are viewers by default
                have = role or Role.viewer
                if have >= needed and (role or needed is Role.viewer):
                    self.assertLess(status, 400, response.get_data(True))
                    self.assertNotIn(self.url('auth.login'),
                                     response.headers.get('Location', ''))
                elif role is not None:
                    self.assertEqual(status, 403)
                elif target.startswith('/api/'):
                    self.assertEqual(status, 401)
                else:
                    self.assertEqual(status, 302)
                    self.assertIn(self.url('auth.login'),
                                  response.headers['Location'])

    def test_view(self):
        for endpoint in ('index', 'overview', 'component.overview',
                         'api.list_versions'):
            self.check(Role.viewer, 'GET', self.url(endpoint))
        self.check(Role.viewer, 'GET', self.url('component.view',
                                                object=self.c1))

    def test_edit(self):
        for endpoint in ('component.edit', 'component.import_',
                         'versions.new', 'edit_settings'):
            self.check(Role.editor, 'GET', self.url(endpoint))
        self.check(Role.editor, 'GET', self.url('component.delete',
                                                object=self.c1))
        self.check(Role.editor, 'POST', self.url('component.edit'),
                   lambda name: dict(data={'name': name}))
        self.check(Role.editor, 'POST', self.url('component.clone',
                                                 object=self.c1))
        self.check(Role.editor, 'POST', self.url('versions.new'),
                   lambda name: dict(data={'version_tag': f'b_{name}',
                                           'from': 'main'}))
        self.assertEqual(sorted(c.name for c in Component.objects(
            name__in=['editor', 'admin', 'site_admin'])),
            ['admin', 'editor', 'site_admin'])
        self.assertFalse(Component.objects(name__in=['anonymous', 'viewer']))

    def test_versions(self):
        def branch(prefix):
            """ make a branch for each role to delete """
            return lambda name: vc.create_version(prefix + name, 'main') and {}
        self.check(Role.editor, 'POST',
                   lambda name: self.url('versions.delete', f'd_{name}'),
                   branch('d_'))
        self.check(Role.editor, 'DELETE',
                   lambda name: self.url('api.delete_version', f'e_{name}'),
                   branch('e_'))
        self.check(Role.editor, 'POST', self.url('api.create_version'),
                   lambda name: dict(json={'version_tag': f'a_{name}'}))
        # tags need admin
        self.check(Role.admin, 'POST', self.url('versions.new'),
                   lambda name: dict(data={'version_tag': f't_{name}',
                                           'from': 'main', 'type': 'tag'}))
        self.check(Role.admin, 'POST', self.url('api.create_version'),
                   lambda name: dict(json={'version_tag': f'at_{name}',
                                           'from': 'main', 'type': 'tag'}))
        tags = {v.version_tag for v in vc.list_versions() if not v.editable}
        self.assertEqual(tags, {'t', 't_admin', 't_site_admin',
                                'at_admin', 'at_site_admin'})

    def test_admin(self):
        for endpoint in ('admin.index', 'admin.settings',
                         'admin.maintenance_page', 'admin.users',
                         'admin.new_user'):
            self.check(Role.site_admin, 'GET', self.url(endpoint))
        self.check(Role.site_admin, 'POST',
                   self.url('admin.clear_cache'))
        self.assertEqual(self.client.get(self.url('admin.logo')).status_code,
                         404)

    def test_buttons(self):
        view = self.url('component.view', object=self.c1)
        tagview = self.url('component.view', 't', object=self.c1)
        for role, branch, tag in ((None, False, False),
                                  (Role.viewer, False, False),
                                  (Role.editor, True, False)):
            with self.subTest(role=role):
                html = self.html(self.request(role, 'GET', view))
                self.assertEqual('id="deletelink"' in html, branch)
                html = self.html(self.request(role, 'GET', tagview))
                self.assertEqual('id="deletelink"' in html, tag)
        # only admins can make tags
        for role, shown in ((Role.editor, False), (Role.admin, True)):
            with self.subTest(role=role):
                html = self.html(self.request(role, 'GET', self.url('index')))
                self.assertIn('New version', html)
                self.assertEqual("type=tag" in html, shown)
                html = self.html(self.request(role, 'GET',
                                              self.url('versions.new')))
                self.assertEqual('id="type_tag"' in html, shown)
        html = self.html(self.request(Role.viewer, 'GET', self.url('index')))
        self.assertNotIn('New version', html)

    def test_api_csrf(self):
        """ The API has no CSRF tokens, so must refuse the form posts that
        another site could make with a user's cookie
        """
        url = self.url('api.create_version')
        response = self.request(Role.site_admin, 'POST', url,
                                data={'version_tag': 'x'})
        self.assertEqual(response.status_code, 415)
        self.assertIn('error', response.get_json())
        self.assertIsNone(response.headers.get('Access-Control-Allow-Origin'))
        self.assertNotIn('x', {v.version_tag for v in vc.list_versions()})


class TestUserAdmin(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user('root', Role.site_admin)
        self.login('root')

    def test_create(self):
        url = self.url('admin.new_user')
        html = self.html(self.client.get(url))
        self.assertIn('id="userform"', html)
        for form, field in ((dict(name='', role='editor',
                                  password=PASSWORD), 'name'),
                            (dict(name='root', role='editor',
                                  password=PASSWORD), 'name'),
                            (dict(name='u1', role='boss',
                                  password=PASSWORD), 'role'),
                            (dict(name='u1', role='editor',
                                  password='short'), 'password')):
            with self.subTest(form=form):
                response = self.client.post(url, data=form)
                self.assertEqual(response.status_code, 400)
                self.assertIn('is-invalid', self.html(response))
        self.assertEqual(User.objects.count(), 1)
        response = self.client.post(url, data=dict(name=' u1 ', role='editor',
                                                   password=PASSWORD))
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(name='u1')
        self.assertIs(user.role, Role.editor)
        self.assertTrue(user.active)
        html = self.html(self.client.get(self.url('admin.users')))
        self.assertIn('>u1</a>', html)
        self.assertEqual(self.login('u1', client=self.app.test_client())
                         .status_code, 302)

    def test_edit(self):
        user = self.make_user('u1', Role.editor)
        other = self.app.test_client()
        self.login('u1', client=other)
        url = self.url('admin.edit_user', userid=user.id)
        self.assertIn('id="active"', self.html(self.client.get(url)))
        # an empty password is unchanged
        response = self.client.post(url, data=dict(role='admin',
                                                   active='true'))
        self.assertEqual(response.status_code, 302)
        user.reload()
        self.assertIs(user.role, Role.admin)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(other.get(self.url('versions.new')).status_code, 200)
        # a new password logs the user out
        response = self.client.post(url, data=dict(role='admin',
                                                   active='true',
                                                   password='battery staple'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.get(name='u1')
                        .check_password('battery staple'))
        self.assertEqual(other.get(self.url('versions.new')).status_code, 302)
        self.login('u1', 'battery staple', client=other)
        # deactivating does too
        response = self.client.post(url, data=dict(role='admin'))
        self.assertFalse(User.objects.get(name='u1').active)
        self.assertEqual(other.get(self.url('versions.new')).status_code, 302)
        self.assertEqual(self.client.get(self.url('admin.edit_user',
                                                  userid='nonsense'))
                         .status_code, 404)

    def test_last_site_admin(self):
        url = self.url('admin.edit_user', userid=self.admin.id)
        for form in (dict(role='admin', active='true'),
                     dict(role='site_admin')):
            with self.subTest(form=form):
                response = self.client.post(url, data=form)
                self.assertEqual(response.status_code, 400)
                self.assertIn('last active site_admin', self.html(response))
                self.admin.reload()
                self.assertIs(self.admin.role, Role.site_admin)
                self.assertTrue(self.admin.active)
        # with another site_admin, this one can step down
        self.make_user('root2', Role.site_admin)
        response = self.client.post(url, data=dict(role='editor',
                                                   active='true'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(self.url('admin.users')).status_code,
                         403)


class TestEnteredBy(AuthTestCase):
    def test_enteredby(self):
        c1 = Component(name='c1').save()
        self.assertIsNone(c1.enteredby)
        self.make_user('u1', Role.editor)
        self.login('u1')
        url = self.url('component.edit', object=c1)
        # a posted value is ignored
        response = self.client.post(url, data=dict(name='c1',
                                                   enteredby='someone'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Component.objects.get().enteredby, 'u1')
        html = self.html(self.client.get(url))
        self.assertIn('by <span id="enteredby">u1</span>', html)
        html = self.html(self.client.get(self.url('component.view',
                                                  object=c1)))
        self.assertIn('<td class="enteredby">u1</td>', html)
