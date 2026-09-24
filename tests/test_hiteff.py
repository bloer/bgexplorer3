import unittest
from mongoengine import connect, disconnect, ValidationError
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.common import units
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.settings import VersionSettings, SpectrumROI, get_settings
from bgexplorer.models import sourceterm  # need this to get signals registered
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

    def test0_key(self):
        h = HitEfficiency(source='U238', location='c1')
        self.assertIsNotNone(h.key)
        self.assertEqual(h.key, h.id)

    def test_ids(self):
        """ ensure that scalars and spectra are assigned an ID """
        h = HitEfficiency(source="h", location="h",
                scalars=dict(
                    v1="10 +- 1 dru/mBq",
                    v2="<3 dru/mBq"),
                spectra = dict(
                    v1=Histogram(AsymmetricUncertainty.fromcounts(np.arange(10))*units('dru/mBq')),
                ))
        h.save()
        h = HitEfficiency.objects.get()
        self.assertEqual(h.scalars['v1'].id, '.'.join([str(h.id), 'v', 'v1']))
        self.assertEqual(h.scalars['v2'].id, '.'.join([str(h.id), 'v', 'v2']))
        self.assertEqual(h.spectra['v1'].hist.id, '.'.join([str(h.id), 's', 'v1']))

    def test_unit_settings(self):
        """ test that unit settings are updated on save and that conflicting
        units cause an error
        """
        h = HitEfficiency(source='h', location='h', scalars=dict(v1='10 +- 1 dru/mBq'))
        h.save()
        cfg = get_settings(h.active_version)
        display_scalars = cfg.hiteffdbconfig.display_scalars
        self.assertEqual(len(display_scalars), 1)
        self.assertIn('v1', display_scalars)
        self.assertTrue(display_scalars['v1'].display_unit.is_compatible_with('dru'))
        with self.assertRaises(ValidationError):
            HitEfficiency(source='h2', location='h2', scalars=dict(v1='3 Hz')).save()

    def test_rois(self):
        config = get_settings(HitEfficiency.get_default_tag())
        config.hiteffdbconfig.rois = [
            SpectrumROI(spectrum='v1', start=0*units.keV, stop=3*units.keV),
            SpectrumROI(spectrum='v1', start=0*units.keV, stop=10*units.keV, mode='average'),
            SpectrumROI(spectrum='v1', start=0*units.keV, stop=10*units.keV, mode='integrate'),
        ]
        config.save()

        h = HitEfficiency(source="h", location="h", scalars=dict(
            v1="10 +- 1 dru/mBq",
            v2="<3 dru/mBq"), spectra = dict(
            v1=Histogram(AsymmetricUncertainty.fromcounts(np.array([0,0,0,0,3,4,5,6,7,8]))*units('dru/mBq'),
                         np.array([0,1,2,3,4,5,6,7,8,9,10])*units.keV),
            ))
        h.save()
        for index, roi in enumerate(config.hiteffdbconfig.rois):
            self.assertIn(roi.key, h.rois)
            result = h.rois[roi.key]
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

        # test that things get updated when we change the config
        config.hiteffdbconfig.rois.pop()
        config.hiteffdbconfig.rois[1].stop = 7*units.keV
        config.save()
        h.reload()
        self.assertEqual(len(h.rois), 2)
        result = h.rois[config.hiteffdbconfig.rois[1].key]
        self.assertAlmostEqual(result.mode, 12/7)
        self.assertAlmostEqual(result.s0, np.sqrt(12)/7)
        self.assertAlmostEqual(result.s1, np.sqrt(12)/7)


