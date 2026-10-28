import unittest
import numpy as np
from numpy.testing import *
from bgexplorer.models.asymmetric import (AsymmetricUncertainty, LinearExpression,
                                         InMemoryLeafStore, sum_values,
                                         CorrelationsNotLoaded)
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

    def test5b_overlapping_leaves(self):
        """ the leaf index gives the same variance as per-leaf derivatives """
        rng = np.random.default_rng(3)
        leaves = [AsymmetricUncertainty(rng.uniform(1, 5), rng.uniform(.1, 1),
                                        rng.uniform(.1, 1))
                  for _ in range(6)]
        total = 0
        for _ in range(12):
            pick = rng.choice(len(leaves), size=rng.integers(1, 4),
                              replace=False)
            term = rng.uniform(-2, 2)
            for i in pick:
                term = term * leaves[i]
            total = total + term
        total = total + 1.5
        expr = total.expression
        mode = sum(np.prod([v.mode for v in key]) * coeff
                   for key, coeff in expr.coefficients.items())
        v0 = v1 = 0
        for variable in expr.variables:
            d = expr.partialderivative(variable).evaluate()
            lo, hi = ((variable.v0, variable.v1) if d.mode >= 0
                      else (variable.v1, variable.v0))
            v0 += (d.modesq + d.v0 / 2) * lo
            v1 += (d.modesq + d.v1 / 2) * hi
        self.assertAlmostEqual(total.mode, mode)
        self.assertAlmostEqual(total.s0, np.sqrt(v0))
        self.assertAlmostEqual(total.s1, np.sqrt(v1))

    def test5c_sum(self):
        """ a one-pass sum is the same as adding one by one """
        x = self.a * self.b
        values = [x, self.a, 3 * self.a + self.c, self.b * self.c, 2.5,
                  self.lim1]
        expected = values[0]
        for v in values[1:]:
            expected = expected + v
        result = AsymmetricUncertainty.sum_all(values)
        self.assertAlmostEqual(result.mode, expected.mode)
        self.assertAlmostEqual(result.s0, expected.s0)
        self.assertAlmostEqual(result.s1, expected.s1)
        # inputs aren't changed
        self.assertAlmostEqual(x.mode, 50)
        self.assertEqual(self.a.expression.coefficients,
                         AsymmetricUncertainty(10, 0.2, id=self.a.id
                                               ).expression.coefficients)
        with AsymmetricUncertainty.ignore_correlations():
            plain = AsymmetricUncertainty.sum_all(values)
            expected = values[0] + values[1] + values[2] + values[3] + \
                values[4] + values[5]
        self.assertAlmostEqual(plain.s0, expected.s0)
        # arrays are not changed in place
        arr = AsymmetricUncertainty(np.array([1., 2.]), np.ones(2),
                                    id='arr')
        total = AsymmetricUncertainty.sum_all([arr, arr, 1])
        assert_allclose(total.mode, [3, 5])
        assert_allclose(arr.mode, [1, 2])

    def test5d_sum_values(self):
        self.assertIsNone(sum_values([]))
        self.assertIsNone(sum_values([None, None]))
        self.assertIs(sum_values([None, self.a]), self.a)
        # pint quantities are converted to the first one's units
        q = sum_values([self.a * units.kg, None, self.b * units.g,
                        self.a * units.g])
        self.assertEqual(q.u, units.kg)
        expected = self.a + self.b / 1000 + self.a / 1000
        self.assertAlmostEqual(q.m.mode, expected.mode)
        self.assertAlmostEqual(q.m.s0, expected.s0)
        with self.assertRaises(pint.DimensionalityError):
            sum_values([self.a * units.kg, self.b * units.m])
        # anything else is added up
        self.assertEqual(sum_values([1, None, 2]), 3)

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

    def test9_format(self):
        # (mode, s0, s1): {format_spec: expected}
        cases = {
            (12.3, 0.3, 9): {
                '': '12.30 +9.00 -0.30',
                'S': '12.30(9.00|0.30)',
                'L': r'12.30^{+9.00}_{-0.30}',
                'LS': r'12.30\left(^{9.00}_{0.30}\right)',
                '.1u': '12.3 +9.0 -0.3',
            },
            (12.34, 0.12, 0.35): {
                '': '12.34 +0.35 -0.12',
                'S': '12.34(35|12)',
                'LS': r'12.34\left(^{35}_{12}\right)',
            },
            # errors within 10%: symmetric, rounded by the average
            (12.3, 0.30, 0.32): {
                '': '12.30+/-0.31',
                'S': '12.30(31)',
                'L': r'12.30 \pm 0.31',
            },
            # ...but not if the average is more than half the mode
            (0.5, 0.30, 0.32): {
                '': '0.50 +0.32 -0.30',
                'S': '0.50(32|30)',
            },
            (1.2e6, 3.4e5, 5e4): {
                '': '(1.20 +0.05 -0.34)e+06',
                'P': '(1.20 +0.05 -0.34)×10⁶',
                'S': '1.20(5|34)e+06',
                'L': r'\left(1.20^{+0.05}_{-0.34}\right) \times 10^{6}',
                'LS': r'1.20\left(^{\phantom{0}5}_{34}\right) \times 10^{6}',
            },
            (0.00123, 0.0003, 0.009): {
                '': '0.00123 +0.00900 -0.00030',
                'S': '0.00123(900|30)',
                'LS': r'0.00123\left(^{900}_{\phantom{0}30}\right)',
            },
            (12.3, 0.96, 3): {
                '': '12.3 +3.0 -1.0',
                'S': '12.3(3.0|1.0)',
            },
            (1234.5, 0.3, 950): {
                '': '1234.50 +950.00 -0.30',
                'L': r'1234.50^{+950.00}_{-\phantom{00}0.30}',
            },
            (-4.2, 0.5, 1.1): {'': '-4.2 +1.1 -0.5'},
            (5, 0, 2): {'': '5.0 +2.0 -0'},
        }
        for args, specs in cases.items():
            for spec, expected in specs.items():
                with self.subTest(args=args, spec=spec):
                    self.assertEqual(format(AsymmetricUncertainty(*args), spec),
                                     expected)

    def test9_format_roundtrip(self):
        from bgexplorer.models.fields import QuantityField
        field = QuantityField()
        for args in ((12.3, 0.3, 9), (1.2e6, 3.4e5, 5e4), (-4.2, 0.5, 1.1),
                     (0.00123, 0.0003, 0.009)):
            with self.subTest(args=args):
                val = AsymmetricUncertainty(*args)
                result = field._fromstr(str(val)).m
                for attr in ('mode', 's0', 's1'):
                    self.assertAlmostEqual(getattr(result, attr),
                                           getattr(val, attr), delta=0.01 *
                                           max(abs(val.mode), val.s1))


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


class TestSerializeExpressions(unittest.TestCase):
    """ Calculated values can be stored with the variables they depend on """
    def roundtrip(self, value, store=None):
        return AsymmetricUncertainty.deserialize(
            value.serialize(), expression=value.serialize_expression(
                store=store), store=store)

    def test_roundtrip(self):
        x = AsymmetricUncertainty(10, 1)
        y = AsymmetricUncertainty(5, 2)
        z = self.roundtrip(x + 2*y - x)
        self.assertAlmostEqual(z.mode, 10)
        self.assertAlmostEqual(z.s0, 4)
        # it's still correlated with y, and doesn't depend on x
        self.assertAlmostEqual((z - 2*y).s0, 0)
        self.assertAlmostEqual((z + x).s0, np.hypot(4, 1))
        self.assertEqual(len(json_terms(x + 2*y - x)['leaves']), 1)
        # leaves have no expression
        self.assertIsNone(x.serialize_expression())

    def test_shared_leaves(self):
        """ values stored separately are correlated through their leaves """
        rate = AsymmetricUncertainty(10, 1)
        a, b = self.roundtrip(rate * 2), self.roundtrip(rate * 3)
        self.assertAlmostEqual((a + b).s0, 5)
        # with a store, the leaves aren't repeated
        store = InMemoryLeafStore()
        c = (rate * 2).serialize_expression(store=store)
        self.assertNotIn('leaves', c)
        self.assertEqual(list(store.leaves), [rate.id])
        a = self.roundtrip(rate * 2, store)
        b = self.roundtrip(rate * 3, store)
        self.assertAlmostEqual((a + b).s0, 5)
        with self.assertRaises(KeyError):
            LinearExpression.from_terms(c)

    def test_arrays(self):
        x = AsymmetricUncertainty(np.arange(3.), np.ones(3))
        y = AsymmetricUncertainty(2, 0.5)
        z = self.roundtrip(x * y + x * np.array([1., 2., 3.]))
        assert_allclose(z.mode, np.arange(3.) * 2 + np.arange(3.) * [1, 2, 3])
        expected = x * y + x * np.array([1., 2., 3.])
        assert_allclose(z.s0, expected.s0)
        assert_allclose((z - expected).s0, 0, atol=1e-12)

    def test_not_loaded(self):
        z = AsymmetricUncertainty.without_correlations(10, 4)
        self.assertFalse(z.correlations_loaded)
        self.assertEqual(z.mode, 10)
        self.assertEqual(str(z), str(AsymmetricUncertainty(10, 4)))
        with self.assertRaises(CorrelationsNotLoaded):
            z + AsymmetricUncertainty(1, 1)
        with self.assertRaises(CorrelationsNotLoaded):
            AsymmetricUncertainty(1, 1) + z
        # scaling by constants is fine, e.g. to convert units
        for scaled, s0 in ((z * 2, 8), (z / 2, 2), (-z, 4), (z + 1, 4)):
            self.assertAlmostEqual(scaled.s0, s0)
            self.assertFalse(scaled.correlations_loaded)
        self.assertAlmostEqual((z * units('kg')).to('g').m.s0, 4000)
        with self.assertRaises(CorrelationsNotLoaded):
            (z * 2) + AsymmetricUncertainty(1, 1)
        # and so is anything when ignoring correlations
        with AsymmetricUncertainty.ignore_correlations():
            self.assertAlmostEqual((z + AsymmetricUncertainty(0, 3)).s0, 5)


def json_terms(value):
    """ serialize_expression, through json like a store would """
    import json
    return json.loads(json.dumps(value.serialize_expression()))
