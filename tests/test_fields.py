import unittest
from unittest import mock
from mongoengine import connect, disconnect, Document,ValidationError
import numpy as np
from numpy.testing import *
import pint
import warnings
from bgexplorer.models.fields import *
from bgexplorer.models.common import units, DimensionalityError
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.histogram import Histogram
from tests.dbutil import connect_test_db

class TestDoc(Document):
    __test__ = False  # not a test case, don't let pytest collect it
    val = QuantityField()
    qval = QuantityField(units='s')
    uval = UncertainQuantityField()
    hval = HistogramField()


class HistUnitsDoc(Document):
    hist = HistogramField(units='1/keV', binsunit='keV', allownone=True)


class TestFields(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        TestDoc.drop_collection()
        self.test = TestDoc()

    def tearDown(self):
        del self.test

    def test1_assign(self):
        self.test.val = 1
        self.assertIsInstance(self.test.val, pint.Quantity)

        self.test.val = AsymmetricUncertainty(10, 1)
        self.assertIsInstance(self.test.val, pint.Quantity)
        self.assertIsInstance(self.test.val.m, AsymmetricUncertainty)

        self.test.uval = 1
        self.assertIsInstance(self.test.val.m, AsymmetricUncertainty)

    def test1_units(self):
        self.test.qval = 20
        self.assertEqual(self.test.qval.u, units.s)
        with self.assertRaises(ValidationError):
            self.test.qval = 10*units.kg
            self.test.validate()


    def test2_convert(self):
        self.test.qval = 10*units.day
        self.assertEqual(self.test.qval.u, units.day)
        self.assertEqual(self.test.qval.m, 10)

        class Test(Document):
            val = QuantityField(units="s", convert=True)

        test = Test(val=10*units.minute)
        self.assertEqual(test.val.u, units.s)
        self.assertEqual(test.val.m, 600)

    def assertStoredAs(self, fromstr):
        """ The value is stored as typed, with its id if it's uncertain """
        stored = TestDoc.val.to_mongo(self.test.val)
        if isinstance(self.test.val.m, AsymmetricUncertainty):
            self.assertEqual(stored, {'str': fromstr,
                                      'id': self.test.val.m.id})
        else:
            self.assertEqual(stored, fromstr)

    def test3_fromstr(self):
        fromstr = "10"
        self.test.val = fromstr
        self.assertStoredAs(fromstr)

        fromstr = "10 +/- 1"
        self.test.val = fromstr
        self.assertStoredAs(fromstr)
        # and loads as the same variable, still shown as typed
        loaded = TestDoc.val.to_python(TestDoc.val.to_mongo(self.test.val))
        self.assertEqual(loaded.m.id, self.test.val.m.id)
        self.assertEqual(get_fromstr(loaded), fromstr)
        self.assertIsInstance(self.test.val.m, AsymmetricUncertainty)
        self.assertEqual(self.test.val.mode, 10)
        self.assertEqual(self.test.val.s0, 1)

        fromstr = "10 +- 1"
        self.test.val = fromstr
        self.assertStoredAs(fromstr)
        self.assertIsInstance(self.test.val.m, AsymmetricUncertainty)
        self.assertEqual(self.test.val.mode, 10)
        self.assertEqual(self.test.val.s0, 1)


        fromstr = "10+1-2"
        self.test.val = fromstr
        self.assertStoredAs(fromstr)
        self.assertIsInstance(self.test.val.m, AsymmetricUncertainty)
        self.assertEqual(self.test.val.mode, 10)
        self.assertEqual(self.test.val.s1, 1)
        self.assertEqual(self.test.val.s0, 2)

        fromstr = "< 10"
        self.test.val = fromstr
        self.assertStoredAs(fromstr)
        self.assertIsInstance(self.test.val.m, AsymmetricUncertainty)
        self.assertAlmostEqual(self.test.val.get_upper_limit(), 10)

        fromstr = "10 keV"
        self.test.val = fromstr
        self.assertStoredAs(fromstr)
        self.assertEqual(self.test.val.u, units.keV)

        fromstr = "<10 keV"
        self.test.val = fromstr
        self.assertStoredAs(fromstr)
        self.assertEqual(self.test.val.u, units.keV)

        fromstr = "10 +/- 2 keV"
        self.test.val = fromstr
        self.assertStoredAs(fromstr)
        self.assertEqual(self.test.val.u, units.keV)

        with self.assertRaises(ValueError):
            self.test.val = "<10 +/- 3"

    def test3_get_fromstr(self):
        self.test.val = "10 +/- 3"
        self.assertEqual(get_fromstr(self.test.val), "10 +/- 3")
        self.assertIsNone(get_fromstr(10 * units.kg))
        self.assertIsNone(get_fromstr(None))
        self.assertIsNone(get_fromstr(AsymmetricUncertainty(1, 1)))
        # getattr on a miss formats the whole magnitude, which is very slow
        # for big arrays. get_fromstr shouldn't
        big = units.Quantity(AsymmetricUncertainty(np.ones(10000), 1), 'kg')
        with mock.patch.object(AsymmetricUncertainty, '__format__',
                               side_effect=AssertionError("formatted")):
            self.assertIsNone(get_fromstr(big))
            # nor should any other attribute miss on a Quantity
            self.assertFalse(hasattr(big, '_fromstr'))

    def test4_hist(self):
        self.test.hval = Histogram(np.arange(20))
        self.assertIsInstance(self.test.hval, Histogram)
        self.assertIsInstance(self.test.hval.hist, pint.Quantity)
        self.assertIsInstance(self.test.hval.hist.m, AsymmetricUncertainty)
        self.assertIsInstance(self.test.hval.hist.mode, np.ndarray)

    def test4_hist_units(self):
        doc = HistUnitsDoc()
        # None is allowed
        doc.validate()
        doc.hist = Histogram(np.arange(3.), np.arange(4.))
        self.assertEqual(doc.hist.bin_edges.u, units.keV)
        self.assertEqual(doc.hist.hist.u, units('1/keV'))
        doc.validate()
        # histogram values are checked
        doc.hist.hist = units.Quantity(AsymmetricUncertainty(np.arange(3.), 0), 'kg')
        with self.assertRaises(ValidationError):
            doc.validate()
        # bins are checked
        doc.hist = Histogram(np.arange(3.) / units.keV,
                             units.Quantity(np.arange(4.), 'kg'))
        with self.assertRaises(ValidationError) as cm:
            doc.validate()
        self.assertIn('keV', str(cm.exception.errors))

    def test5_json(self):
        test = TestDoc(val=1, qval=2, uval=AsymmetricUncertainty(3,1),
                       hval=Histogram(np.arange(50)))
        warnings.simplefilter('ignore')
        json = test.to_json()
        test2 = TestDoc.from_json(json)
        self.assertEqual(test.val, test2.val)
        self.assertEqual(test.qval, test2.qval)
        self.assertAlmostEqual(test.uval.mode, test2.uval.mode)
        self.assertAlmostEqual(test.uval.s0, test2.uval.s0)
        assert_equal(test.hval.hist.mode, test2.hval.hist.mode)

        json = """{
            "val": {"value": 1},
            "qval": {"value": 2, "units": "s"},
            "uval": {"value": 3, "sigma": 1},
            "hval": {"value": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,
                14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28,
                29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43,
                44, 45, 46, 47, 48, 49],
                "bins": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
                16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30,
                31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45,
                46, 47, 48, 49, 50]}}"""

        test2 = TestDoc.from_json(json)
        self.assertEqual(test.val, test2.val)
        self.assertEqual(test.qval, test2.qval)
        self.assertAlmostEqual(test.uval.mode, test2.uval.mode)
        self.assertAlmostEqual(test.uval.s0, test2.uval.s0)
        assert_equal(test.hval.hist.mode, test2.hval.hist.mode)


    def test5_save(self):
        test = TestDoc(val=1, qval=2, uval=AsymmetricUncertainty(3,1),
                       hval=Histogram(np.arange(50)))
        test.save()
        test2 = TestDoc.objects.get()
        self.assertEqual(test.val, test2.val)
        self.assertEqual(test.qval, test2.qval)
        self.assertAlmostEqual(test.uval.mode, test2.uval.mode)
        self.assertAlmostEqual(test.uval.s0, test2.uval.s0)
        assert_equal(test.hval.hist.mode, test2.hval.hist.mode)

    def test6_array_storage(self):
        hist = Histogram(AsymmetricUncertainty(np.arange(50.), np.ones(50),
                                               np.full(50, 2.)),
                         np.arange(51.))
        test = TestDoc(val=units.Quantity(np.arange(3.), 'kg'), hval=hist)
        test.save()
        raw = TestDoc._get_collection().find_one()
        # arrays are stored as raw bytes
        self.assertEqual(raw['hval']['value']['dtype'], '<f8')
        self.assertEqual(raw['hval']['value']['shape'], [50])
        self.assertNotIn('zlib', raw['hval']['value'])
        self.assertEqual(raw['val']['value']['shape'], [3])
        test2 = TestDoc.objects.get()
        assert_equal(test2.hval.hist.mode, hist.hist.mode)
        assert_equal(test2.hval.hist.s0, hist.hist.s0)
        assert_equal(test2.hval.hist.s1, hist.hist.s1)
        assert_equal(test2.hval.bin_edges.m, np.arange(51.))
        assert_equal(test2.val.m, np.arange(3.))
        self.assertEqual(test2.val.u, units.kg)
        # decoded arrays are writeable, but values of AUs are immutable
        self.assertTrue(decode_array(encode_array(np.arange(3.)))
                        .flags.writeable)
        with self.assertRaises(ValueError):
            test2.hval.hist.mode[0] = 5

        # big arrays are compressed
        test = TestDoc(hval=Histogram(np.zeros(10000))).save()
        raw = TestDoc._get_collection().find_one({'_id': test.id})
        self.assertTrue(raw['hval']['value']['zlib'])
        assert_equal(TestDoc.objects.get(id=test.id).hval.hist.mode,
                     np.zeros(10000))

    def test6_array_encoding(self):
        for arr in (np.arange(12.).reshape(3, 4), np.arange(5, dtype='>i4'),
                    np.ones((200, 200)), np.arange(10.)[::2]):
            decoded = decode_array(encode_array(arr))
            assert_equal(decoded, arr)
            self.assertEqual(decoded.dtype, arr.dtype)
        self.assertEqual(encode_array(np.float64(3)), 3)

    def test6_legacy_storage(self):
        """ npz blobs and plain lists written by older versions still load """
        import io
        buf = io.BytesIO()
        np.savez_compressed(buf, value=np.arange(3.), sigma=np.ones(3),
                            bins=np.arange(4.), units='keV')
        coll = TestDoc._get_collection()
        npzid = coll.insert_one({'hval': buf.getvalue()}).inserted_id
        coll.insert_one({'hval': {'value': [0., 1., 2.],
                                            'sigma': [1., 1., 1.],
                                            'bins': [0., 1., 2., 3.]}})
        for doc in TestDoc.objects:
            assert_equal(doc.hval.hist.mode, np.arange(3.))
            assert_equal(doc.hval.hist.s0, np.ones(3))
            assert_equal(doc.hval.bin_edges.m, np.arange(4.))
        self.assertEqual(TestDoc.objects.get(id=npzid).hval.hist.u, units.keV)
