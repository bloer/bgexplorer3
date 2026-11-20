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

    @staticmethod
    def reference(h, a, b, binwidth, average):
        """ The weights bin by bin, the way they were once calculated: all of
        the bins in [a, b], and fractions of the first and last
        """
        edges = h.bin_edges.to('keV').m
        a = max(a.to('keV').m, edges[0])
        b = min(b.to('keV').m, edges[-1])
        first = np.searchsorted(edges, a, "right") - 1
        last = np.searchsorted(edges, b, "left") - 1
        weights = np.zeros(len(edges) - 1)
        weights[first:last] = 1
        if first == last:
            weights[first] = (b - a) / (edges[first+1] - edges[first])
        else:
            weights[first] = ((edges[first+1] - a)
                              / (edges[first+1] - edges[first]))
            weights[last] = (b - edges[last]) / (edges[last+1] - edges[last])
        if binwidth:
            weights = weights * np.diff(edges)
        if average:
            weights = weights / (b - a)
        return weights

    def test6b_weights_match_reference(self):
        """ The weights, values and units are those of a bin by bin
        calculation, for random bins and ranges
        """
        rng = np.random.default_rng(3)
        checked = 0
        for trial in range(60):
            n = int(rng.integers(3, 40))
            edges = (np.sort(rng.uniform(0, 3000, n + 1)) if trial % 2
                     else np.linspace(0, 3000, n + 1))
            counts = (np.zeros(n) if trial % 5 == 0
                      else rng.poisson(rng.uniform(0, 5), n).astype(float))
            h = Histogram(AsymmetricUncertainty.fromcounts(counts)
                          * units('dru/mBq'), edges * units.keV)
            for i in range(8):
                a, b = sorted(rng.uniform(-200, 3300, 2))
                if i == 0:
                    a = edges[int(rng.integers(0, n))]
                if i == 1:
                    b = edges[int(rng.integers(1, n + 1))]
                scale, unit = ((1, units.keV) if i % 2 else (1e-3, units.MeV))
                a, b = a * scale * unit, b * scale * unit
                if not (a.to('keV').m < b.to('keV').m
                        and a.to('keV').m < edges[-1]
                        and b.to('keV').m > edges[0]):
                    continue
                for binwidth in (True, False):
                    for method, average in (('integrate', False),
                                            ('average', True)):
                        weights = h.roi_weights(a, b, binwidth, average)
                        assert_array_almost_equal(
                            weights,
                            self.reference(h, a, b, binwidth, average))
                        if not weights.any():
                            continue
                        result = getattr(h, method)(a, b, binwidth)
                        expected = h.hist.m.integrate(weights)
                        power = int(binwidth) - int(average)
                        self.assertEqual(
                            result.u, units('dru/mBq').u * units.keV ** power)
                        assert_array_almost_equal(
                            [result.m.mode, result.m.s0, result.m.s1],
                            [expected.mode, expected.s0, expected.s1])
                        checked += 1
        self.assertGreater(checked, 500)

    def test6c_no_bins_included(self):
        with self.assertRaises(ValueError):
            self.h.integrate(50, 60)
        with self.assertRaises(ValueError):
            self.h.integrate(15, 25, exclude=[(0, 100)])
        self.assertFalse(self.h.roi_weights(50, 60).any())

    def test6d_exclude(self):
        h = self.h
        # bins of 10 with contents 1, 2, 3
        self.assertAlmostEqual(h.integrate(exclude=[(20, 30)]), 40)
        self.assertAlmostEqual(h.integrate(15, 35, exclude=[(20, 30)]), 5 + 15)
        # a part of a bin
        self.assertAlmostEqual(h.integrate(exclude=[(12, 15)]), 60 - 3)
        # inside one bin, and a range that spans bin edges
        self.assertAlmostEqual(h.integrate(exclude=[(22, 24), (26, 34)]),
                               60 - 4 - 2 * 4 - 3 * 4)
        # the sum doesn't depend on the order or overlaps of the ranges
        self.assertAlmostEqual(
            h.integrate(exclude=[(26, 34), (12, 15), (22, 28), (14, 16)]),
            h.integrate(exclude=[(12, 16), (22, 34)]))
        # parts outside the range are ignored
        self.assertAlmostEqual(
            h.integrate(15, 25, exclude=[(0, 12), (24, 99)]),
            15 - 2 * 1)
        # the average is over the included width only
        self.assertAlmostEqual(h.average(exclude=[(20, 30)]), 40 / 20)
        self.assertAlmostEqual(h.average(10, 40, exclude=[(10, 20), (30, 40)]),
                               2)
        # without bin widths, the sum of the included fractions
        self.assertAlmostEqual(h.integrate(binwidth=False,
                                           exclude=[(15, 20), (25, 30)]),
                               0.5 + 1 + 3)
        self.assertAlmostEqual(h.average(binwidths=False, exclude=[(20, 30)]),
                               4 / 20)

    def test6e_exclude_with_units(self):
        """ Excluded ranges are converted to the units of the bins """
        n = 20
        edges = np.linspace(0, 100, n + 1) * units.keV
        rng = np.random.default_rng(5)
        counts = rng.poisson(4, n).astype(float)
        h = Histogram(AsymmetricUncertainty.fromcounts(counts) * units.dru,
                      edges)
        excluded = [(12.5 * units.keV, 31 * units.keV),
                    (0.05 * units.MeV, 0.0625 * units.MeV)]
        result = h.integrate(5 * units.keV, 90 * units.keV, exclude=excluded)
        whole = h.integrate(5 * units.keV, 90 * units.keV)
        parts = [h.integrate(lo, hi) for lo, hi in excluded]
        self.assertAlmostEqual(result.m.mode,
                               whole.m.mode - sum(p.m.mode for p in parts))
        self.assertEqual(result.u, whole.u)
        with self.assertRaises(DimensionalityError):
            h.integrate(exclude=[(1 * units.kg, 2 * units.kg)])

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
