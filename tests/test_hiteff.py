import unittest
from mongoengine import connect, disconnect
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.common import units
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.settings import VersionSettings, SpectrumROI, get_settings
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
        VersionSettings.drop_collection()

    def tearDown(self):
        pass

    def test_ids(self):
        """ ensure that values and spectra are assigned an ID """
        h = HitEfficiency(source="h", location="h", values=dict(
                    v1="10 +- 1 dru/mBq",
                    v2="<3 dru/mBq"),
                spectra = dict(
                    v1=Histogram(AsymmetricUncertainty.fromcounts(np.arange(10))*units('dru/mBq')),
                ))
        h.save()
        h = HitEfficiency.objects.get()
        self.assertEqual(h.values['v1'].id, '.'.join([str(h.id), 'v', 'v1']))
        self.assertEqual(h.values['v2'].id, '.'.join([str(h.id), 'v', 'v2']))
        self.assertEqual(h.spectra['v1'].hist.id, '.'.join([str(h.id), 's', 'v1']))

    def test_rois(self):
        config = get_settings(HitEfficiency.get_default_tag())
        config.hiteffdbconfig.rois = [
            SpectrumROI(spectrum='v1', start=0*units.keV, stop=3*units.keV),
            SpectrumROI(spectrum='v1', start=0*units.keV, stop=10*units.keV, mode='average'),
            SpectrumROI(spectrum='v1', start=0*units.keV, stop=10*units.keV, mode='integrate'),
        ]
        config.save()

        h = HitEfficiency(source="h", location="h", values=dict(
            v1="10 +- 1 dru/mBq",
            v2="<3 dru/mBq"), spectra = dict(
            v1=Histogram(AsymmetricUncertainty.fromcounts(np.array([0,0,0,0,3,4,5,6,7,8]))*units('dru/mBq'),
                         np.array([0,1,2,3,4,5,6,7,8,9,10])*units.keV),
            ))
        h.save()
        for index, roi in enumerate(config.hiteffdbconfig.rois):
            self.assertIn(roi.key, h.values)
            result = h.values[roi.key]
            if index == 0:  # average over 0 bins
                self.assertTrue(result.check('dru/mBq'))
                self.assertEqual(result.mode, 0)
                self.assertEqual(result.s0, 0)
                self.assertAlmostEqual(result.s1, h.spectra['v1'].hist.s1[0]/3)
            elif index == 1:  # average over all
                self.assertTrue(result.check('dru/mBq'))
                self.assertAlmostEqual(result.mode, 3.3)
                self.assertAlmostEqual(result.s0, np.sqrt(33)/10)
                self.assertAlmostEqual(result.s1, np.sqrt(33)/10)
            elif index == 2:  # integral over all
                self.assertTrue(result.check('1/g'))
                self.assertAlmostEqual(result.mode, 33)
                self.assertAlmostEqual(result.v0, 33)
                self.assertAlmostEqual(result.v1, 33)


