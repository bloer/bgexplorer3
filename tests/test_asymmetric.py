import unittest
import numpy as np
from numpy.testing import *
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.common import units, pint

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

    def test6_lazy(self):
        """ test that intermediate objects are not evaluated """
        i1 = self.a * self.lim1
        i2 = self.b * self.lim2
        result = i1 + i2
        # reading the mode property triggers evaluate on result, but not i1,i2
        self.assertEqual(result.mode, 0)
        self.assertIsNotNone(result.s0)
        self.assertIsNone(i1._mode)
        self.assertIsNone(i2._mode)

    def test_dimensionless_bug(self):
        """ in some contexts quantities aren't constructed correctly? """
        self.assertTrue(hasattr(pint.Quantity(AsymmetricUncertainty(10,1), 'mBq/kg'), 'dimensionality'))

    def test_ufunc_bug(self):
        """ test that numpy operations return AsymmetricUncertainties """
        x = np.ones_like(self.arr)
        ops = [np.add, np.subtract, np.multiply, np.true_divide]
        for op in ops:
            with AsymmetricUncertainty.ignore_correlations():
                y = op(self.arr, x)
            self.assertIsInstance(y, AsymmetricUncertainty)
            self.assertIsInstance(y.mode, np.ndarray)
            self.assertIsInstance(y.s0[0], float)

            y = op(self.arr, x)
            self.assertIsInstance(y, AsymmetricUncertainty)
            self.assertIsInstance(y.mode, np.ndarray)
            self.assertIsInstance(y.s0[0], float)





    def test7_ppf(self):
        """ ppf should match the scipy distribution """
        from scipy.stats import norm, halfnorm
        # upper limit
        ul = AsymmetricUncertainty(0, 0, 2)
        self.assertAlmostEqual(ul.ppf(0.9), halfnorm(0, 2).ppf(0.9))
        # symmetric: same as a normal distribution
        sym = AsymmetricUncertainty(5, 1)
        for q in (0.05, 0.5, 0.9):
            self.assertAlmostEqual(sym.ppf(q), norm(5, 1).ppf(q))
        # asymmetric: each side is a scaled half of a normal distribution
        asym = AsymmetricUncertainty(5, 1, 3)
        self.assertAlmostEqual(asym.ppf(asym.qlow), 5)
        self.assertAlmostEqual(asym.ppf(0.1), norm(5, 1).ppf(0.1/0.25/2))
        self.assertAlmostEqual(asym.ppf(0.9), norm(5, 3).ppf(1.4/1.5))
        # arrays
        arr = AsymmetricUncertainty(np.array([0, 5, 5.]), np.array([0, 1, 1]),
                                    np.array([2, 1, 3]))
        assert_allclose(arr.ppf(0.9), [ul.ppf(0.9), sym.ppf(0.9),
                                       asym.ppf(0.9)])

    def test8_serialize(self):
        arr = AsymmetricUncertainty(np.arange(3.), np.ones(3), np.full(3, 2.))
        for val in (arr, AsymmetricUncertainty(np.arange(3.), 1),
                    AsymmetricUncertainty(2, 1, 3)):
            for q in (val, val * units.keV):
                ser = AsymmetricUncertainty.serializeq(q)
                result = AsymmetricUncertainty.deserialize(ser)
                assert_equal(result.mode, q.mode)
                assert_equal(result.s0, q.s0)
                assert_equal(result.s1, q.s1)
                self.assertEqual(getattr(result, 'units', None),
                                 getattr(q, 'units', None))
        # legacy npz archive
        import io
        buf = io.BytesIO()
        np.savez_compressed(buf, *arr.serialize(compressarrays=False))
        result = AsymmetricUncertainty.deserialize((buf.getvalue(), 'keV'))
        assert_equal(result.s1, arr.s1)
        self.assertEqual(result.u, units.keV)


class TestLeaves(unittest.TestCase):
    """ Independent variables have ids and can't change """
    def test_ids(self):
        a = AsymmetricUncertainty(10, 1)
        self.assertTrue(a.isleaf)
        self.assertIsInstance(a.id, str)
        self.assertNotEqual(a.id, AsymmetricUncertainty(10, 1).id)
        # results of calculations aren't variables of their own
        result = a * 2 + AsymmetricUncertainty(3, 1)
        self.assertFalse(result.isleaf)
        self.assertIsNone(result.id)
        # leaves with the same id are the same variable
        copy = AsymmetricUncertainty(10, 1, id=a.id)
        self.assertEqual(copy, a)
        self.assertAlmostEqual((a - copy).s0, 0)
        # multiplying by 1, e.g. by pint for units, keeps the variable
        self.assertIs(a * 1, a)
        self.assertIs((a * units('kg')).m, a)

    def test_unique_across_threads(self):
        from concurrent.futures import ThreadPoolExecutor

        def make(n):
            return [AsymmetricUncertainty(1, 1).id for _ in range(n)]
        with ThreadPoolExecutor(8) as pool:
            ids = [i for chunk in pool.map(make, [2000] * 8) for i in chunk]
        self.assertEqual(len(set(ids)), len(ids))

    def test_immutable(self):
        a = AsymmetricUncertainty(10, 1)
        for attr in ('mode', 's0', 's1', 'id'):
            with self.assertRaises(AttributeError):
                setattr(a, attr, 3)
        arr = np.arange(3.)
        b = AsymmetricUncertainty(arr, np.ones(3))
        # the values are a copy, which can't be changed
        arr[0] = 5
        self.assertEqual(b.mode[0], 0)
        with self.assertRaises(ValueError):
            b.mode[0] = 5
        with self.assertRaises(ValueError):
            b.s1[0] = 5
        # nor can evaluated results
        result = b * 2 + b
        with self.assertRaises(ValueError):
            result.mode[0] = 5
        # methods that adjust values return new ones
        lim = AsymmetricUncertainty(np.zeros(2), np.zeros(2), np.ones(2))
        zeroed = lim.rezero()
        assert_equal(zeroed.s1, [0, 0])
        assert_equal(lim.s1, [1, 1])
