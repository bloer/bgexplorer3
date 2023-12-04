import unittest
from mongoengine import connect, disconnect
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.common import units
from bgexplorer.models.asymmetric import AsymmetricError
from bgexplorer.models.histogram import Histogram
import numpy as np

class TestHitEFficiency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # TODO: try to use mongo, and if it fails, switch to monomock
        # and add an expected failure for all $merge pipelines
        connect(uuidRepresentation='standard')
        #connect('mongoenginetest', host='mongodb://localhost',
        #        mongo_client_class=mongomock.MongoClient,
        #        uuidRepresentation='stanard')

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        HitEfficiency.drop_collection()

    def tearDown(self):
        pass

    def test_ids(self):
        """ ensure that values and spectra are assigned an ID """
        h = HitEfficiency(source="h", location="h", values=dict(
            v1="10 +- 1 dru/mBq",
            v2="<3 dru/mBq"), spectra = dict(
            v1=Histogram(AsymmetricError.fromcounts(np.arange(10))*units('dru/mBq')),
            ))
        h.save()
        h = HitEfficiency.objects.get()
        self.assertEqual(h.values['v1'].id, '.'.join([str(h.id), 'v', 'v1']))
        self.assertEqual(h.values['v2'].id, '.'.join([str(h.id), 'v', 'v2']))
        self.assertEqual(h.spectra['v1'].hist.id, '.'.join([str(h.id), 's', 'v1']))
