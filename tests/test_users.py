""" User accounts at the model layer """
import unittest
from mongoengine import disconnect, NotUniqueError
from bgexplorer.models.users import User, Role
from bgexplorer.models.component import Component
from bgexplorer.models.verdoc import set_user_provider
from bgexplorer.models.settings import VersionSettings
from bgexplorer.models import versioncontrol as vc
from tests.dbutil import connect_test_db


class TestUsers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        User.drop_collection()

    def test_password(self):
        user = User(name='u1')
        self.assertFalse(user.check_password('anything'))
        user.set_password('correct horse')
        self.assertNotIn('correct horse', user.pwhash)
        user.save()
        user = User.objects.get(name='u1')
        self.assertTrue(user.check_password('correct horse'))
        self.assertFalse(user.check_password('wrong horse'))
        self.assertFalse(user.check_password(''))
        self.assertFalse(user.check_password(None))
        with self.assertRaises(ValueError):
            user.set_password('short')
        # a corrupt hash is just wrong
        user.pwhash = 'not a hash'
        self.assertFalse(user.check_password('correct horse'))

    def test_roles(self):
        self.assertTrue(Role.viewer < Role.editor < Role.admin
                        < Role.site_admin)
        user = User(name='u1').save()
        self.assertIs(User.objects.get(name='u1').role, Role.viewer)
        user.role = Role.admin
        self.assertTrue(user.has_role(Role.editor))
        self.assertTrue(user.has_role(Role.admin))
        self.assertFalse(user.has_role(Role.site_admin))
        user.active = False
        self.assertFalse(user.has_role(Role.viewer))

    def test_unique(self):
        User(name='u1').save()
        with self.assertRaises(NotUniqueError):
            User(name='u1').save()

    def test_enteredby(self):
        for cls in (Component, VersionSettings):
            cls.drop_collection()
        vc.create_version('main')
        name = None
        set_user_provider(lambda: name)
        try:
            c1 = Component(name='c1', enteredby='someone').save()
            self.assertEqual(c1.enteredby, 'someone')
            name = 'u1'
            c1.save()
            self.assertEqual(Component.objects.get().enteredby, 'u1')
            name = None
            c1.save()
            self.assertEqual(Component.objects.get().enteredby, 'u1')
        finally:
            set_user_provider(None)


if __name__ == '__main__':
    unittest.main()
