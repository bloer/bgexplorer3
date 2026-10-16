import unittest
import numpy as np
from numpy.testing import assert_array_equal, assert_array_almost_equal
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.asymmetric import AsymmetricUncertainty, sum_values
from bgexplorer.models.common import units, DimensionalityError


class TestHistogram(unittest.TestCase):
    def setUp(self):
        self.h = Histogram(np.array([1., 2., 3.]),
                           np.array([10., 20., 30., 40.]))

    def test1_binary_operators(self):
        h = self.h
        assert_array_equal((h + h).hist, [2, 4, 6])
        assert_array_equal((h - h).hist, [0, 0, 0])
        assert_array_equal((h * 2).hist, [2, 4, 6])
        assert_array_equal((h / 2).hist, [0.5, 1, 1.5])
        assert_array_equal((h // 2).hist, [0, 1, 1])
        assert_array_equal((h % 2).hist, [1, 0, 1])
        assert_array_equal((h ** 2).hist, [1, 4, 9])
        # bins are not combined
        assert_array_equal((h + h).bin_edges, h.bin_edges)
        assert_array_equal((h ** 2).bin_edges, h.bin_edges)

    def test2_inplace_operators(self):
        for op, expected in [('__iadd__', [2, 3, 4]),
                             ('__isub__', [0, 1, 2]),
                             ('__imul__', [1, 2, 3]),
                             ('__itruediv__', [1, 2, 3]),
                             ('__ipow__', [1, 2, 3]),
                             ]:
            h = Histogram(self.h.hist.copy(), self.h.bin_edges)
            result = getattr(h, op)(1)
            self.assertIs(result, h, op)
            assert_array_equal(h.hist, expected, op)
        h = Histogram(self.h.hist.copy(), self.h.bin_edges)
        h -= h
        assert_array_equal(h.hist, [0, 0, 0])

    def test3_reverse_unary(self):
        h = self.h
        assert_array_equal((1 - h).hist, [0, -1, -2])
        assert_array_equal((2 ** h).hist, [2, 4, 8])
        assert_array_equal((-h).hist, [-1, -2, -3])
        assert_array_equal(abs(-h).hist, [1, 2, 3])

    def test4_mismatched_bins(self):
        other = Histogram(np.array([1., 2.]), np.array([0., 1., 2.]))
        with self.assertRaises(ValueError):
            self.h + other

    def test5_val(self):
        h = self.h
        self.assertEqual(h.find_bin(15), 0)
        self.assertEqual(h.val(15), 1)
        self.assertEqual(h.val(20), 2)
        self.assertEqual(h.val(39), 3)
        self.assertIsNone(h.val(5))
        self.assertIsNone(h.val(40))
        self.assertAlmostEqual(h.val(15, interp=True), 1.5)
        # can't interpolate beyond the last bin
        self.assertEqual(h.val(35, interp=True), 3)

    def test6_integrate(self):
        h = self.h
        self.assertAlmostEqual(h.integrate(), 60)
        self.assertAlmostEqual(h.integrate(binwidth=False), 6)
        self.assertAlmostEqual(h.integrate(15, 25), 15)
        self.assertAlmostEqual(h.integrate(0, 100), 60)
        self.assertAlmostEqual(h.average(), 2)
        self.assertAlmostEqual(h.average(20, 30), 2)

    def test7_default_bins(self):
        h = Histogram(np.array([1., 2.]))
        assert_array_almost_equal(h.bin_edges, [0, 1, 2])

    def uncertain(self, unit, *leaves):
        """ A Histogram in `unit` with a bin made of each of `leaves` and
        its scalings. The bins are one array-valued AsymmetricUncertainty
        """
        mode = np.array([leaf.mode * (i + 1) + 1
                         for i, leaf in enumerate(leaves)])
        sigma = np.array([leaf.s0 * (i + 1) for i, leaf in enumerate(leaves)])
        value = AsymmetricUncertainty(mode, sigma, sigma, id=str(leaves))
        return Histogram(units.Quantity(value, unit),
                         units.Quantity(np.arange(len(leaves) + 1.), 'keV'))

    def test8_sum_all(self):
        a, b, c = (AsymmetricUncertainty(m, 0.1 * m, id=i)
                   for m, i in [(10., 'a'), (5., 'b'), (2., 'c')])
        hists = [self.uncertain('dru', a, b, c),
                 self.uncertain('dru', b, b, a),
                 self.uncertain('mdru', c, a, c)]
        expected = hists[0] + hists[1] + hists[2]
        total = sum_values(hists)
        self.assertIsInstance(total, Histogram)
        self.assertEqual(total.hist.u, units.dru)
        assert_array_equal(total.bin_edges.m, hists[0].bin_edges.m)
        for field in ('mode', 's0', 's1'):
            assert_array_almost_equal(getattr(total.hist.m, field),
                                      getattr(expected.hist.m, field))
        # correlations are kept
        diff = sum_values([hists[0], -hists[0]])
        assert_array_almost_equal(diff.hist.m.mode, 0)
        assert_array_almost_equal(diff.hist.m.s1, 0)
        # the inputs are unchanged
        assert_array_almost_equal(hists[0].hist.m.mode, [11, 11, 7])
        # others are added as before
        assert_array_equal(sum_values([self.h, self.h, self.h]).hist, [3, 6, 9])
        # units must match and so must bins
        with self.assertRaises(DimensionalityError):
            sum_values([hists[0], self.uncertain('kg', a, b, c)])
        with self.assertRaises(ValueError):
            sum_values([hists[0], self.uncertain('dru', a, b)])


if __name__ == '__main__':
    unittest.main()
