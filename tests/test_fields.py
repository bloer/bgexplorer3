import unittest
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

    def test3_fromstr(self):
        fromstr = "10"
        self.test.val = fromstr
        self.assertEqual(TestDoc.val.to_mongo(self.test.val), fromstr)

        fromstr = "10 +/- 1"
        self.test.val = fromstr
        self.assertEqual(TestDoc.val.to_mongo(self.test.val), fromstr)
        self.assertIsInstance(self.test.val.m, AsymmetricUncertainty)
        self.assertEqual(self.test.val.mode, 10)
        self.assertEqual(self.test.val.s0, 1)

        fromstr = "10 +- 1"
        self.test.val = fromstr
        self.assertEqual(TestDoc.val.to_mongo(self.test.val), fromstr)
        self.assertIsInstance(self.test.val.m, AsymmetricUncertainty)
        self.assertEqual(self.test.val.mode, 10)
        self.assertEqual(self.test.val.s0, 1)


        fromstr = "10+1-2"
        self.test.val = fromstr
        self.assertEqual(TestDoc.val.to_mongo(self.test.val), fromstr)
        self.assertIsInstance(self.test.val.m, AsymmetricUncertainty)
        self.assertEqual(self.test.val.mode, 10)
        self.assertEqual(self.test.val.s1, 1)
        self.assertEqual(self.test.val.s0, 2)

        fromstr = "< 10"
        self.test.val = fromstr
        self.assertEqual(TestDoc.val.to_mongo(self.test.val), fromstr)
        self.assertIsInstance(self.test.val.m, AsymmetricUncertainty)
        self.assertAlmostEqual(self.test.val.get_upper_limit(), 10)

        fromstr = "10 keV"
        self.test.val = fromstr
        self.assertEqual(TestDoc.val.to_mongo(self.test.val), fromstr)
        self.assertEqual(self.test.val.u, units.keV)

        fromstr = "<10 keV"
        self.test.val = fromstr
        self.assertEqual(TestDoc.val.to_mongo(self.test.val), fromstr)
        self.assertEqual(self.test.val.u, units.keV)

        fromstr = "10 +/- 2 keV"
        self.test.val = fromstr
        self.assertEqual(TestDoc.val.to_mongo(self.test.val), fromstr)
        self.assertEqual(self.test.val.u, units.keV)

        with self.assertRaises(ValueError):
            self.test.val = "<10 +/- 3"

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
