import unittest
import numpy as np
from numpy.testing import *
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.common import units

class TestAsymmetric(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        self.a = AsymmetricUncertainty(10, 0.2)
        self.b = AsymmetricUncertainty(5, 0.05)
        self.c = AsymmetricUncertainty(8, 1, 0.1)
        self.lim1 = AsymmetricUncertainty.fromlimit(3)
        self.lim2 = AsymmetricUncertainty.fromlimit(5)
        self.arr = AsymmetricUncertainty.fromcounts(np.array([4,5,6]))
        self.constant = 24
        self.scalar = 2.5


    def tearDown(self):
        pass

    def test1_add(self):
        x = self.a + self.constant
        self.assertIsInstance(x, AsymmetricUncertainty)
        self.assertAlmostEqual(x.mode, self.a.mode + self.constant)
        self.assertEqual(x.s0, self.a.s0)

        x = self.arr + self.constant
        self.assertIsInstance(x.mode, np.ndarray)
        assert_allclose(x.mode, self.arr.mode + self.constant)
        assert_equal(x.s0, self.arr.s0)

        x = self.a + self.b
        self.assertAlmostEqual(x.mode, self.a.mode + self.b.mode)
        self.assertAlmostEqual(x.s0, np.sqrt(self.a.v0 + self.b.v0))

        x = self.a + self.c
        self.assertAlmostEqual(x.mode, self.a.mode + self.c.mode)
        self.assertAlmostEqual(x.s0, np.sqrt(self.a.v0 + self.c.v0))
        self.assertAlmostEqual(x.s1, np.sqrt(self.a.v1 + self.c.v1))

        x = self.arr + self.a
        self.assertIsInstance(x.mode, np.ndarray)
        assert_allclose(x.mode, self.arr.mode + self.a.mode)
        assert_allclose(x.s0, np.sqrt(self.arr.v0 + self.a.v0))

        x = self.a + self.lim1
        self.assertEqual(x.mode, self.a.mode)
        self.assertEqual(x.s0, self.a.s0)
        self.assertAlmostEqual(x.s1, np.sqrt(self.a.v1 + self.lim1.v1))

        x = self.lim1 + self.lim2
        self.assertTrue(x.isupperlimit())
        self.assertAlmostEqual(x.get_upper_limit(),
                               np.sqrt(self.lim1.get_upper_limit()**2 +
                                       self.lim2.get_upper_limit()**2))

    def test2_sub(self):
        x = self.a - self.constant
        self.assertIsInstance(x, AsymmetricUncertainty)
        self.assertAlmostEqual(x.mode, self.a.mode - self.constant)
        self.assertEqual(x.s0, self.a.s0)

        x = self.arr - self.constant
        self.assertIsInstance(x.mode, np.ndarray)
        assert_allclose(x.mode, self.arr.mode - self.constant)
        assert_equal(x.s0, self.arr.s0)

        x = self.a - self.b
        self.assertAlmostEqual(x.mode, self.a.mode - self.b.mode)
        self.assertAlmostEqual(x.s0, np.sqrt(self.a.v0 + self.b.v0))

        x = self.a - self.c
        self.assertAlmostEqual(x.mode, self.a.mode - self.c.mode)
        self.assertAlmostEqual(x.s0, np.sqrt(self.a.v0 + self.c.v1))
        self.assertAlmostEqual(x.s1, np.sqrt(self.a.v1 + self.c.v0))

    def test3_mult(self):
        x = self.a * self.scalar
        self.assertIsInstance(x, AsymmetricUncertainty)
        self.assertAlmostEqual(x.mode, self.a.mode * self.scalar)
        self.assertAlmostEqual(x.s0, self.a.s0 * self.scalar)

        x = self.a * self.b
        self.assertAlmostEqual(x.mode, self.a.mode * self.b.mode)
        self.assertAlmostEqual(x.s0, np.sqrt(self.a.modesq * self.b.v0 +
                                             self.b.modesq * self.a.v0 +
                                             self.a.v0 * self.b.v0))

        x = self.arr * self.scalar
        assert_allclose(x.mode, self.arr.mode * self.scalar)
        assert_allclose(x.s0, self.arr.s0 * self.scalar)

        x = self.arr * self.a
        assert_allclose(x.mode, self.arr.mode * self.a.mode)
        assert_allclose(x.s0, np.sqrt(self.arr.modesq * self.a.v0 +
                                      self.a.modesq * self.arr.v0 +
                                      self.arr.v0 * self.a.v0))

        x = self.lim1 * self.scalar
        self.assertTrue(x.isupperlimit())
        self.assertAlmostEqual(x.get_upper_limit(),
                               self.lim1.get_upper_limit() * self.scalar)

        x = self.a * self.lim1
        self.assertTrue(x.isupperlimit())
        # what should the value actually be here?

    def test3_mult_units(self):
        x = self.a * units.keV
        self.assertIsInstance(x, units.Quantity)
        self.assertIsInstance(x.m, AsymmetricUncertainty)
        self.assertEqual(x.mode, self.a.mode)
        self.assertEqual(x.s0, self.a.s0)

        q = units('30 keV')
        x = self.a * q
        self.assertIsInstance(x, units.Quantity)
        self.assertIsInstance(x.m, AsymmetricUncertainty)
        self.assertAlmostEqual(x.mode, self.a.mode * q.m)
        self.assertAlmostEqual(x.s0, self.a.s0 * q.m)

    def test4_div(self):
        x = self.a / self.scalar
        self.assertIsInstance(x, AsymmetricUncertainty)
        self.assertAlmostEqual(x.mode, self.a.mode / self.scalar)
        self.assertAlmostEqual(x.s0, self.a.s0 / self.scalar)

    def test5_correlation(self):
        x = self.a + (3 * self.a)
        y = 4 * self.a
        self.assertEqual(x.expression, y.expression)
        self.assertAlmostEqual(x.mode, y.mode)
        self.assertAlmostEqual(x.s0, y.s0)

        x = self.a * self.b + self.a * self.c
        y = self.a * (self.b + self.c)
        with AsymmetricUncertainty.ignore_correlations():
            z = self.a * self.b + self.a * self.c
        self.assertAlmostEqual(x.mode, y.mode)
        self.assertAlmostEqual(x.s0, y.s0)
        self.assertAlmostEqual(x.s1, y.s1)
        self.assertAlmostEqual(x.mode, z.mode)
        self.assertGreater(x.s0, z.s0)
        self.assertGreater(x.s1, z.s1)

