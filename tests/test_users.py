""" User accounts at the model layer """
import unittest
from mongoengine import disconnect, NotUniqueError
from bgexplorer.models.users import User, Role
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


if __name__ == '__main__':
    unittest.main()
