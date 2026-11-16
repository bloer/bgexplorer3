""" Shared by the jobs tests. Spawned worker processes import this module to
register the kinds below, see `init` """
import datetime
import unittest
import mongoengine
from bgexplorer import jobs
from bgexplorer.jobs import models
from tests.dbutil import connect_test_db, TEST_MONGODB_URI


def later(seconds):
    return models.utcnow() + datetime.timedelta(seconds=seconds)


class JobTestCase(unittest.TestCase):
    """ A clean jobs collection for each test """
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        mongoengine.disconnect()

    def setUp(self):
        models.Job.drop_collection()
        models.Job.ensure_indexes()

    def status(self, job_id):
        return models.get(job_id).status
