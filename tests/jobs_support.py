""" Shared by the jobs tests. Spawned worker processes import this module to
register the kinds below, see `init` """
import datetime
import os
import unittest
import mongoengine
from bgexplorer import jobs
from bgexplorer.jobs import models
from tests.dbutil import connect_test_db, TEST_MONGODB_URI


def later(seconds):
    return models.utcnow() + datetime.timedelta(seconds=seconds)


def init(uri=TEST_MONGODB_URI):
    """ Initializer for worker processes: connect to the database. Importing
    this module has registered the kinds """
    mongoengine.connect(host=uri, uuidRepresentation='standard',
                        serverSelectionTimeoutMS=2000)


@jobs.job_kind('ps_add')
def _add(params):
    jobs.progress(1, 2, stage='adding')
    jobs.progress(2, 2)
    return {'sum': params['a'] + params['b'], 'pid': os.getpid()}


@jobs.job_kind('ps_boom')
def _boom(params):
    raise ValueError('process failure\nsecond line')


@jobs.job_kind('ps_die')
def _die(params):
    os._exit(1)


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
