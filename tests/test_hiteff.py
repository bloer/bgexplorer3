import unittest
from mongoengine import connect, disconnect, ValidationError
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.common import units
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.settings import (VersionSettings, SpectrumROI,
                                        get_settings, HitEffConfig)
from bgexplorer.models import sourceterm  # need this to get signals registered
import numpy as np
from tests.dbutil import connect_test_db


class TestHitEFficiency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

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
        ids = [h.scalars['v1'].m.id, h.scalars['v2'].m.id,
               h.spectra['v1'].hist.m.id]
        self.assertEqual(len(set(ids)), 3)
        h.save()
        # they're the same variables whenever they're loaded
        for loaded in (HitEfficiency.objects.get(),
                       HitEfficiency.objects.get()):
            self.assertEqual([loaded.scalars['v1'].m.id,
                              loaded.scalars['v2'].m.id,
                              loaded.spectra['v1'].hist.m.id], ids)

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




class TestPartialLoad(unittest.TestCase):
    """ Saving a hit efficiency loaded without its spectra keeps them """
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        from bgexplorer.models.versioncontrol import create_version
        HitEfficiency.drop_collection()
        VersionSettings.drop_collection()
        config = get_settings('main')
        config.hiteffdbconfig.rois = [
            SpectrumROI(spectrum='v1', start=0*units.keV, stop=5*units.keV,
                        mode='integrate')]
        config.save()
        self.h = HitEfficiency(
            source='h', location='h', scalars=dict(v1='10 +- 1 dru/mBq'),
            spectra=dict(v1=Histogram(
                AsymmetricUncertainty.fromcounts(np.arange(10))
                * units('dru/mBq'), np.arange(11) * units.keV))).save()
        self.roi = self.h.rois[config.hiteffdbconfig.rois[0].key]
        create_version('b', 'main')

    def check_intact(self, tag):
        h = HitEfficiency.select_version(tag).get()
        self.assertEqual(list(h.spectra), ['v1'])
        self.assertEqual(h.spectra_keys, ['v1'])
        (roi,) = h.rois.values()
        self.assertEqual(str(roi), str(self.roi))
        return h

    def test_is_loaded(self):
        h = HitEfficiency.select_version('main').exclude('spectra').get()
        self.assertFalse(h.is_loaded('spectra'))
        self.assertTrue(h.is_loaded('scalars'))
        h = HitEfficiency.select_version('main').only('source').get()
        self.assertTrue(h.is_loaded('source'))
        self.assertFalse(h.is_loaded('scalars'))
        h = HitEfficiency.select_version('main').first()
        self.assertTrue(h.is_loaded('spectra'))
        h = HitEfficiency.select_version('main').exclude('spectra').get()
        h.reload()
        self.assertTrue(h.is_loaded('spectra'))

    def test_active_version_not_dynamic(self):
        """ setting active_version doesn't create a saved dynamic field """
        coll = HitEfficiency._get_collection()
        h = HitEfficiency.select_version('b').get()
        self.assertEqual(h.active_version, 'b')
        self.assertNotIn('active_version', h._fields_ordered)
        h.source = 'h2'
        h.save()
        for doc in coll.find():
            self.assertNotIn('active_version', doc)
        # stale values saved by older versions are ignored
        coll.update_many({}, {'$set': {'active_version': 'nope'}})
        h = HitEfficiency.select_version('b').get()
        self.assertEqual(h.active_version, 'b')
        self.assertNotIn('active_version', h._dynamic_fields)

    def test_save_without_spectra(self):
        for tag in ('main', 'b'):  # 'b' shares the document: copy on write
            with self.subTest(tag=tag):
                h = HitEfficiency.select_version(tag).exclude('spectra').get()
                h.scalars['v2'] = AsymmetricUncertainty(3, 1) * units('dru/mBq')
                h.save()
                h = self.check_intact(tag)
                self.assertEqual(sorted(h.scalars_keys), ['v1', 'v2'])


class TestSpectrumParsing(unittest.TestCase):
    def test_columns(self):
        text = """# a comment
        low, high, value, sigma
        0, 1, 5, 1
        1, 2, 3, 0.5  # trailing comment

        2, 4, 1, 0.1
        """
        h = Histogram.from_columns(text, 'dru/mBq', 'keV')
        np.testing.assert_allclose(h.bin_edges.m, [0, 1, 2, 4])
        self.assertEqual(h.bin_edges.u, units.keV)
        np.testing.assert_allclose(h.hist.m.nominal_value, [5, 3, 1])
        self.assertTrue(h.hist.u.is_compatible_with('dru/mBq'))
        # whitespace, asymmetric, no units
        h = Histogram.from_columns("0 1 5 1 2\n1 2 3 1 2\n")
        self.assertEqual(len(h.hist), 2)
        self.assertTrue(h.hist.dimensionless)

    def test_bad_columns(self):
        for text, msg in (("0 1 5\n2 3 1\n", "contiguous"),
                          ("0 1\n", "columns"),
                          ("0 1 5\n1 2 3 4\n", "columns"),
                          ("1 0 5\n", "high edge"),
                          ("0 1 5\n1 2 x\n", "Line 2"),
                          ("# nothing\n", "No data")):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, msg):
                    Histogram.from_columns(text)

    def test_dict(self):
        h = Histogram.from_dict(dict(value=[1, 2], sigma=[0.1, 0.2],
                                     bins=[0, 1, 3], units='dru/mBq',
                                     binsunit='keV'))
        np.testing.assert_allclose(h.bin_edges.m, [0, 1, 3])
        self.assertTrue(h.hist.u.is_compatible_with('dru/mBq'))
        # units as defaults, no sigma
        h = Histogram.from_dict(dict(value=[1, 2], bins=[0, 1, 3]),
                                'dru/mBq', 'keV')
        self.assertEqual(h.bin_edges.u, units.keV)
        with self.assertRaises(ValueError):
            Histogram.from_dict(dict(value=[1, 2]))
        with self.assertRaises(ValueError):
            Histogram.from_dict(dict(value=[1, 2], bins=[0, 1]))


class TestSpectrumEditing(TestPartialLoad):
    """ add, rename and remove spectra; ROIs follow """
    def spectrum(self, value=1, unit='dru/mBq'):
        return Histogram(AsymmetricUncertainty(np.full(10, value * 1.),
                                               np.zeros(10)) * units(unit),
                         np.arange(11) * units.keV)

    def roi_of(self, h):
        (roi,) = h.rois.values()
        return roi

    def test_add_rename_remove(self):
        h = HitEfficiency.select_version('b').get()
        h.add_spectrum('v2', self.spectrum())
        h = HitEfficiency.select_version('b').get()
        self.assertEqual(sorted(h.spectra_keys), ['v1', 'v2'])
        with self.assertRaises(KeyError):
            h.add_spectrum('v2', self.spectrum())
        # the ROI uses spectrum v1; replacing it changes the ROI
        h.add_spectrum('v1', self.spectrum(2), overwrite=True)
        h = HitEfficiency.select_version('b').get()
        self.assertAlmostEqual(self.roi_of(h).m.nominal_value, 10)
        with self.assertRaises(KeyError):
            h.rename_spectrum('v1', 'v2')
        with self.assertRaises(KeyError):
            h.rename_spectrum('nope', 'x')
        h.rename_spectrum('v1', 'renamed')
        h = HitEfficiency.select_version('b').get()
        self.assertEqual(sorted(h.spectra_keys), ['renamed', 'v2'])
        self.assertIsNone(self.roi_of(h))
        h.rename_spectrum('v2', 'v1')
        h.remove_spectrum('renamed')
        h = HitEfficiency.select_version('b').get()
        self.assertEqual(h.spectra_keys, ['v1'])
        self.assertAlmostEqual(self.roi_of(h).m.nominal_value, 5)
        with self.assertRaises(KeyError):
            h.remove_spectrum('renamed')
        # main is untouched
        self.check_intact('main')

    def test_units_and_loading(self):
        config = get_settings('b')
        config.hiteffdbconfig.display_spectra = dict(
            v2=HitEffConfig(display_unit='dru/mBq'))
        config.save()
        h = HitEfficiency.select_version('b').get()
        with self.assertRaises(ValidationError):
            h.add_spectrum('v2', self.spectrum(unit='keV'))
        h = HitEfficiency.select_version('b').exclude('spectra').get()
        with self.assertRaises(ValueError):
            h.add_spectrum('v3', self.spectrum())
        self.check_intact('b')
