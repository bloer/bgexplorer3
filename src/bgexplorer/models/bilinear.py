""" Sums of products of two kinds of independent variables, evaluated with
sparse matrices.

A background budget adds up terms of the form `weight * rate * efficiency`,
where the rates and the efficiencies are each shared by many terms. As a
`LinearExpression` that is one key per (rate, efficiency) pair and a
recursive evaluation that builds an expression per leaf, which is slow and
needs memory for every intermediate value. Collecting the weights in a sparse
matrix W of rates by efficiencies gives the same result with a few matrix
products:

    mode = (W^T r) . h
    var  = sum_j [(W^T r)_j^2 + sum_s W_sj^2 vr_s] vh_j  +  sum_s vr_s (W h)_s^2

where r, vr are the modes and variances of the rates and h, vh those of the
efficiencies. It is the same formula as `LinearExpression.evaluate`, to
rounding, and the efficiencies may be arrays (such as spectra), evaluated
all at once. Everything is assumed to be non-negative: weights, modes and
variances, so that the lower variance of the sum only comes from the lower
variances of its parts.
"""
from collections import defaultdict
from typing import Callable, Optional, Tuple
import numpy as np
from scipy import sparse
from .asymmetric import AsymmetricUncertainty, LinearExpression

# modes, lower variances and upper variances
Moments = Tuple[np.ndarray, np.ndarray, np.ndarray]


def bilinear_sum(rows, cols, weights, rate: Moments, hit: Moments
                 ) -> Moments:
    """ The mode and lower and upper standard deviations of
    `sum_i weights[i] * R[rows[i]] * H[cols[i]]`, for independent variables
    R with `rate` moments (mode, v0, v1 arrays by row index) and H with `hit`
    moments, which have a shape (m,) or (m, k), for any number k of values at
    once. The entries (row, col, weight) may repeat, and are added. All
    must be non-negative. The result has the shape of one row of `hit`.
    """
    rows = np.asarray(rows)
    hit = tuple(np.asarray(a, dtype=float) for a in hit)
    shape = hit[0].shape[1:]
    if not len(rows):
        zero = np.zeros(shape)
        return zero, zero.copy(), zero.copy()
    # only the rates and efficiencies in use
    used_rows, row_index = np.unique(rows, return_inverse=True)
    used_cols, col_index = np.unique(cols, return_inverse=True)
    w = sparse.coo_matrix(
        (np.asarray(weights, dtype=float), (row_index, col_index)),
        shape=(len(used_rows), len(used_cols))).tocsr()
    rmode, rv0, rv1 = (np.asarray(a, dtype=float)[used_rows] for a in rate)
    hmode, hv0, hv1 = (a[used_cols].reshape(len(used_cols), -1) for a in hit)

    # the rates' modes per efficiency: the efficiencies' derivatives
    b = w.T @ rmode
    mode = b @ hmode
    # the rates' derivatives
    u = w @ hmode
    w2 = w.multiply(w).T.tocsr()
    result = [mode.reshape(shape)]
    for rvar, hvar in ((rv0, hv0), (rv1, hv1)):
        c = b * b + w2 @ rvar
        var = c @ hvar + rvar @ (u * u)
        result.append(np.sqrt(var).reshape(shape))
    return tuple(result)


def bilinear_each(terms, nterms: int, rows, cols, weights, rate: Moments,
                  hit: Moments) -> Moments:
    """ `bilinear_sum` for each of `nterms` separate sums at once, with the
    entries (rows, cols, weights) of the sum `terms[i]` (from 0 to
    `nterms`-1). The rates and efficiencies are shared by the sums, but
    within each they are independent of those in the other sums, so the sums
    are not added. The result has the shape (nterms,) + one row of `hit`
    """
    hit = tuple(np.asarray(a, dtype=float) for a in hit)
    shape = hit[0].shape[1:]
    if not len(terms):
        return tuple(np.zeros((nterms,) + shape) for _ in range(3))
    terms = np.asarray(terms, dtype=np.int64)
    nrates, nhits = len(rate[0]), len(hit[0])
    r_key, r_index = np.unique(terms * nrates + np.asarray(rows),
                               return_inverse=True)
    c_key, c_index = np.unique(terms * nhits + np.asarray(cols),
                               return_inverse=True)
    w = sparse.coo_matrix(
        (np.asarray(weights, dtype=float), (r_index, c_index)),
        shape=(len(r_key), len(c_key))).tocsr()
    rmode, rv0, rv1 = (np.asarray(a, dtype=float)[r_key % nrates]
                       for a in rate)
    hmode, hv0, hv1 = (a[c_key % nhits].reshape(len(c_key), -1) for a in hit)
    # adds the entries of each sum
    by_hit = sparse.csr_matrix(
        (np.ones(len(c_key)), (c_key // nhits, np.arange(len(c_key)))),
        shape=(nterms, len(c_key)))
    by_rate = sparse.csr_matrix(
        (np.ones(len(r_key)), (r_key // nrates, np.arange(len(r_key)))),
        shape=(nterms, len(r_key)))
    b = w.T @ rmode
    u = w @ hmode
    w2 = w.multiply(w).T.tocsr()
    result = [(by_hit @ (b[:, None] * hmode)).reshape((nterms,) + shape)]
    for rvar, hvar in ((rv0, hv0), (rv1, hv1)):
        c = b * b + w2 @ rvar
        var = by_hit @ (c[:, None] * hvar) + by_rate @ (rvar[:, None] * u * u)
        result.append(np.sqrt(var).reshape((nterms,) + shape))
    return tuple(result)


class LazyExpression(LinearExpression):
    """ A `LinearExpression` whose terms are only made when they are needed,
    by `build`, which returns the `coefficients`. Sums of many terms are
    usually only displayed, and never added to anything else
    """
    def __init__(self, build: Callable[[], dict]):
        self._build = build
        self._coefficients = None

    @property
    def coefficients(self):
        if self._coefficients is None:
            coefficients = defaultdict(self._defaultcoefficient)
            coefficients.update(self._build())
            self._coefficients = coefficients
            self._build = None
        return self._coefficients

    @coefficients.setter
    def coefficients(self, value):
        self._coefficients = value


def bilinear_value(rows, cols, weights, rate: Moments, hit: Moments,
                   leaves: Optional[Callable] = None
                   ) -> AsymmetricUncertainty:
    """ `bilinear_sum` as an AsymmetricUncertainty of scalars (so `hit` has
    the shape (m,)). If `leaves` is given, the value has an expression and
    so is correlated with anything made of the same variables.
    `leaves(rows, cols, weights)` returns the dict for its coefficients: the
    set of the AsymmetricUncertainty variables for each product, with its
    weight
    """
    mode, s0, s1 = bilinear_sum(rows, cols, weights, rate, hit)
    if leaves is None:
        return AsymmetricUncertainty(float(mode), float(s0), float(s1))
    rows, cols = np.asarray(rows), np.asarray(cols)
    weights = np.asarray(weights)
    return AsymmetricUncertainty(
        float(mode), float(s0), float(s1),
        expression=LazyExpression(lambda: leaves(rows, cols, weights)))
