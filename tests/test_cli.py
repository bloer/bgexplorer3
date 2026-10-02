""" The command line tools for user accounts and versions """
import unittest
import mongoengine
from click.testing import CliRunner
from bgexplorer.cli import cli, versions_cli
from bgexplorer.models.users import User, Role
from bgexplorer.models import versioncontrol as vc
from bgexplorer.models.settings import VersionSettings, get_settings
from bgexplorer.models.history import VersionEvent
from bgexplorer.models.versiondiff import diff_versions
from tests.test_versionfile import make_model
from tests.dbutil import TEST_MONGODB_URI, check_test_db

PASSWORD = 'correct horse'


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_test_db()

    def setUp(self):
        # the same connection settings as the commands use
        mongoengine.connect(host=TEST_MONGODB_URI)
        self.addCleanup(mongoengine.disconnect)
        User.drop_collection()

    def invoke(self, *args, input=None):
        # no app or configuration, so no SECRET_KEY
        env = {'FLASK_SECRET_KEY': None, 'BGEXPLORER_CONFIG': None}
        return CliRunner().invoke(cli, ['--uri', TEST_MONGODB_URI, *args],
                                  input=input, env=env)

    def test_create(self):
        result = self.invoke('create', 'root', '--role', 'site_admin',
                             input=f'{PASSWORD}\n{PASSWORD}\n')
        self.assertEqual(result.exit_code, 0, result.output)
        user = User.objects.get(name='root')
        self.assertIs(user.role, Role.site_admin)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertNotIn(PASSWORD, result.output)
        result = self.invoke('create', 'root', '--password', PASSWORD)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('already a user', result.output)
        result = self.invoke('create', 'u2', '--password', 'short')
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('at least 8', result.output)
        result = self.invoke('create', 'u3', '--role', 'boss',
                             '--password', PASSWORD)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(User.objects.count(), 1)
        result = self.invoke('create', 'u4', '--password', PASSWORD)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIs(User.objects.get(name='u4').role, Role.viewer)

    def test_set_password(self):
        user = User(name='u1')
        user.set_password(PASSWORD)
        user.save()
        result = self.invoke('set-password', 'u1',
                             input='battery staple\nbattery staple\n')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(User.objects.get().check_password('battery staple'))
        result = self.invoke('set-password', 'nobody', '--password', PASSWORD)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('No user', result.output)

    def test_uri_env(self):
        result = CliRunner().invoke(
            cli, ['create', 'u1', '--password', PASSWORD],
            env={'FLASK_MONGODB_URI': TEST_MONGODB_URI})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(User.objects.get().name, 'u1')


if __name__ == '__main__':
    unittest.main()


class TestVersionsCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_test_db()

    def setUp(self):
        mongoengine.connect(host=TEST_MONGODB_URI)
        self.addCleanup(mongoengine.disconnect)
        for cls in [VersionSettings, VersionEvent] + vc._versioned_classes:
            cls.drop_collection()
        make_model('src')

    def invoke(self, *args, input=None):
        env = {'FLASK_SECRET_KEY': None, 'BGEXPLORER_CONFIG': None}
        return CliRunner().invoke(versions_cli,
                                  ['--uri', TEST_MONGODB_URI, *args],
                                  input=input, env=env)

    def test_export_import(self):
        with CliRunner().isolated_filesystem():
            result = self.invoke('export', 'src')
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn('3 Component', result.output)
            with open('src.bgx.tar.gz', 'rb') as f:
                self.assertEqual(f.read(2), b'\x1f\x8b')
            result = self.invoke('import', 'src.bgx.tar.gz', '--name', 'copy',
                                 '--tag', '--description', 'from the cli')
            self.assertEqual(result.exit_code, 0, result.output)
            settings = get_settings('copy', create=False)
            self.assertFalse(settings.editable)
            self.assertEqual(settings.description, 'from the cli')
            self.assertFalse(diff_versions('src', 'copy').changed)
            result = self.invoke('list')
            self.assertIn('copy\ttag\tfrom the cli', result.output)
            # refusals
            result = self.invoke('import', 'src.bgx.tar.gz', '--name', 'copy')
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn('already a version', result.output)
            with open('bad.tar.gz', 'wb') as f:
                f.write(b'junk')
            result = self.invoke('import', 'bad.tar.gz', '--name', 'new')
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn('Not a version file', result.output)
            result = self.invoke('export', 'nosuch')
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("No version named 'nosuch'", result.output)

    def test_stdout(self):
        result = CliRunner().invoke(
            versions_cli, ['--uri', TEST_MONGODB_URI, 'export', 'src',
                           '-o', '-'],
            env={'FLASK_SECRET_KEY': None, 'BGEXPLORER_CONFIG': None})
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.stdout_bytes[:2], b'\x1f\x8b')
        result = self.invoke('import', '-', '--name', 'piped',
                             input=result.stdout_bytes)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(vc.version_exists('piped'))
