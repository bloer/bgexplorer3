import unittest
import numpy as np
from bgexplorer.models.asymmetric import (AsymmetricUncertainty as AU,
                                          LinearExpression)
from bgexplorer.models.bilinear import (bilinear_sum, bilinear_value,
                                        LazyExpression)


def moments(leaves):
    return (np.array([x.mode for x in leaves]),
            np.array([x.s0 ** 2 for x in leaves]),
            np.array([x.s1 ** 2 for x in leaves]))


def reference(rows, cols, weights, rates, hits):
    """ The sum as a LinearExpression, the general way """
    expression = LinearExpression()
    for r, c, w in zip(rows, cols, weights):
        expression.coefficients[frozenset({rates[r], hits[c]})] += w
    return expression.evaluate(lazy=False)


def random_case(rng, n=8, m=5, entries=30, shape=()):
    rates = [AU(rng.uniform(0.1, 5), rng.uniform(0.01, 1),
                rng.uniform(0.01, 1)) for _ in range(n)]
    hits = [AU(rng.uniform(0.1, 5, shape) if shape else rng.uniform(0.1, 5),
               rng.uniform(0.01, 1, shape) if shape else rng.uniform(0.01, 1),
               rng.uniform(0.01, 1, shape) if shape else rng.uniform(0.01, 1))
            for _ in range(m)]
    rows = rng.integers(0, n, entries)
    cols = rng.integers(0, m, entries)
    weights = rng.uniform(0.1, 3, entries)
    return rows, cols, weights, rates, hits


class TestBilinearSum(unittest.TestCase):
    def check(self, case):
        rows, cols, weights, rates, hits = case
        mode, s0, s1 = bilinear_sum(rows, cols, weights, moments(rates),
                                    moments(hits))
        expected = reference(rows, cols, weights, rates, hits)
        np.testing.assert_allclose(mode, expected.mode, rtol=1e-10)
        np.testing.assert_allclose(s0, expected.s0, rtol=1e-10)
        np.testing.assert_allclose(s1, expected.s1, rtol=1e-10)

    def test_scalars(self):
        rng = np.random.default_rng(1)
        for _ in range(5):
            self.check(random_case(rng))

    def test_repeated_entries_add(self):
        rates = [AU(2., 0.5, 0.7)]
        hits = [AU(3., 0.2, 0.1)]
        self.check(([0, 0, 0], [0, 0, 0], [1., 2., 0.5], rates, hits))

    def test_single_rate_or_hit(self):
        rng = np.random.default_rng(2)
        rows, cols, weights, rates, hits = random_case(rng)
        self.check((rows, np.zeros_like(cols), weights, rates, hits))
        self.check((np.zeros_like(rows), cols, weights, rates, hits))

    def test_arrays(self):
        rng = np.random.default_rng(3)
        self.check(random_case(rng, shape=(4,)))

    def test_array_columns_match_scalars(self):
        rng = np.random.default_rng(4)
        rows, cols, weights, rates, hits = random_case(rng, shape=(3,))
        mode, s0, s1 = bilinear_sum(rows, cols, weights, moments(rates),
                                    moments(hits))
        for k in range(3):
            single = [AU(h.mode[k], h.s0[k], h.s1[k]) for h in hits]
            one = bilinear_sum(rows, cols, weights, moments(rates),
                               moments(single))
            np.testing.assert_allclose([mode[k], s0[k], s1[k]], one)

    def test_unused_entries_ignored(self):
        rng = np.random.default_rng(5)
        rows, cols, weights, rates, hits = random_case(rng, n=20, m=10,
                                                       entries=6)
        self.check((rows, cols, weights, rates, hits))

    def test_empty(self):
        mode, s0, s1 = bilinear_sum([], [], [], moments([AU(1, 0)]),
                                    moments([AU(1, 0)]))
        self.assertEqual((mode, s0, s1), (0, 0, 0))
        mode, s0, s1 = bilinear_sum([], [], [], moments([AU(1, 0)]),
                                    (np.ones((1, 3)),) * 3)
        self.assertEqual(mode.shape, (3,))
        self.assertFalse(mode.any())

    def test_zero_weights_and_variances(self):
        rates = [AU(2., 0., 0.), AU(1., 0.3, 0.3)]
        hits = [AU(0., 0., 0.5), AU(4., 0., 0.)]
        self.check(([0, 1, 1], [0, 0, 1], [1., 0., 2.], rates, hits))


class TestBilinearValue(unittest.TestCase):
    def leaves(self, rates, hits):
        def build(rows, cols, weights):
            result = {}
            for r, c, w in zip(rows, cols, weights):
                key = frozenset({rates[r], hits[c]})
                result[key] = result.get(key, 0) + w
            return result
        return build

    def test_correlated_like_general_sum(self):
        rng = np.random.default_rng(6)
        rows, cols, weights, rates, hits = random_case(rng)
        half = len(rows) // 2
        build = self.leaves(rates, hits)
        args = moments(rates), moments(hits)
        a = bilinear_value(rows[:half], cols[:half], weights[:half], *args,
                           leaves=build)
        b = bilinear_value(rows[half:], cols[half:], weights[half:], *args,
                           leaves=build)
        whole = reference(rows, cols, weights, rates, hits)
        self.assertAlmostEqual(float(a.nominal_value + b.nominal_value),
                               float(whole.nominal_value))
        # sharing rates and hit efficiencies, the sum is not independent
        total = a + b
        self.assertAlmostEqual(total.mode, whole.mode)
        self.assertAlmostEqual(total.s0, whole.s0)
        self.assertAlmostEqual(total.s1, whole.s1)
        # and the difference is exactly zero in mode and variance
        difference = a - a
        self.assertAlmostEqual(difference.mode, 0)
        self.assertAlmostEqual(difference.s0, 0)

    def test_expression_is_lazy(self):
        built = []

        def build(rows, cols, weights):
            built.append(1)
            return {}
        value = bilinear_value([0], [0], [1.], moments([AU(1, .1)]),
                               moments([AU(1, .1)]), leaves=build)
        self.assertIsInstance(value.expression, LazyExpression)
        self.assertEqual(built, [])
        value.expression.coefficients
        value.expression.coefficients
        self.assertEqual(built, [1])

    def test_without_expression(self):
        rng = np.random.default_rng(7)
        rows, cols, weights, rates, hits = random_case(rng)
        value = bilinear_value(rows, cols, weights, moments(rates),
                               moments(hits))
        whole = reference(rows, cols, weights, rates, hits)
        self.assertAlmostEqual(value.mode, whole.mode)
        self.assertAlmostEqual(value.s1, whole.s1)


if __name__ == '__main__':
    unittest.main()
