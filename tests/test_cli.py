""" The command line tools for user accounts """
import unittest
import mongoengine
from click.testing import CliRunner
from bgexplorer.cli import cli
from bgexplorer.models.users import User, Role
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
