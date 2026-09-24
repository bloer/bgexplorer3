""" Shared MongoDB connection handling for tests

Tests run against a real mongod, since mongomock doesn't support the
$merge aggregations used by version control. Set
BGEXPLORER_TEST_MONGODB_URI to choose the server and database (all
collections in it will be dropped!). If the server can't be reached, tests
that need it are skipped, unless BGEXPLORER_TEST_REQUIRE_DB is set, in which
case they fail.
"""
import os
import unittest
from mongoengine import connect, disconnect
from pymongo.errors import PyMongoError

TEST_MONGODB_URI = os.environ.get(
    'BGEXPLORER_TEST_MONGODB_URI',
    'mongodb://127.0.0.1:27017/bgexplorer3_unittest')
REQUIRE_DB = bool(os.environ.get('BGEXPLORER_TEST_REQUIRE_DB'))

# only wait for the server timeout once per test run
_unavailable = None


def check_test_db():
    """ Raise unittest.SkipTest if the test database can't be reached """
    global _unavailable
    if _unavailable is None:
        try:
            connect_test_db(check=False).admin.command('ping')
            _unavailable = ''
        except PyMongoError as e:
            _unavailable = f"MongoDB not available at {TEST_MONGODB_URI}: {e}"
        finally:
            disconnect()
    if _unavailable:
        if REQUIRE_DB:
            raise RuntimeError(_unavailable)
        raise unittest.SkipTest(_unavailable)


def connect_test_db(check=True):
    """ Connect mongoengine to the test database. If `check`, first make sure
    it's available, see `check_test_db`
    """
    if check:
        check_test_db()
    return connect(host=TEST_MONGODB_URI, uuidRepresentation='standard',
                   serverSelectionTimeoutMS=2000)
