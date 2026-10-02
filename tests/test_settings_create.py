""" Default settings and server secrets are created safely by processes
starting at the same time """
import unittest
from unittest import mock
from mongoengine import disconnect
from mongoengine.connection import get_db
from bgexplorer.models.settings import (VersionSettings, ApplicationSettings,
                                        get_settings,
                                        get_application_settings,
                                        get_server_secret, use_server_secret)
from tests.dbutil import connect_test_db


def lose_race(cls, make):
    """ Make `cls.objects.get` miss the first time, as if `make()` ran in
    another process just after the lookup
    """
    real = cls.objects.get

    def get(*args, **kwargs):
        if not lookups:
            lookups.append(1)
            make()
            raise cls.DoesNotExist()
        return real(*args, **kwargs)
    lookups = []
    return mock.patch.object(type(cls.objects), 'get',
                             side_effect=get, autospec=False)


class TestSettingsCreate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        VersionSettings.drop_collection()
        ApplicationSettings.drop_collection()
        get_db()['server_secrets'].drop()

    def test_version_settings(self):
        with lose_race(VersionSettings,
                       lambda: VersionSettings(version_tag='v').save()):
            settings = get_settings('v')
        self.assertEqual(settings.version_tag, 'v')
        self.assertEqual(VersionSettings.objects.count(), 1)

    def test_application_settings(self):
        with lose_race(ApplicationSettings, get_application_settings):
            settings = get_application_settings()
        self.assertEqual(settings.id, ApplicationSettings.objects.get().id)
        self.assertEqual(ApplicationSettings.objects.count(), 1)

    def test_server_secret(self):
        value = get_server_secret('x')
        self.assertEqual(len(value), 64)
        self.assertEqual(get_server_secret('x'), value)
        self.assertNotEqual(get_server_secret('y'), value)
        self.assertFalse(use_server_secret('x', 'wrong'))
        self.assertFalse(use_server_secret('x', ''))
        self.assertTrue(use_server_secret('x', value))
        self.assertFalse(use_server_secret('x', value))
        self.assertNotEqual(get_server_secret('x'), value)
