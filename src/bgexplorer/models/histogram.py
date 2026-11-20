import operator
import numpy as np
from pint.errors import PintError
from .asymmetric import AsymmetricUncertainty
from .common import units
from .ranges import merge_ranges


def as_quantity(edges):
    """ `edges` as a Quantity, dimensionless if it has no unit """
    return edges if hasattr(edges, 'to') else units.Quantity(
        np.asarray(edges, dtype=float))


def describe_bins(edges) -> str:
    """ Short text for the bins with these edges, like
    '1500 bins from 0 to 3000 keV'
    """
    edges = as_quantity(edges)
    unit = '' if edges.dimensionless else f' {edges.u:~}'
    values = np.asarray(edges.m, dtype=float)
    return (f"{len(values) - 1} bins from {values[0]:g} to {values[-1]:g}"
            f"{unit}")


def check_bin_edges(edges) -> None:
    """ Raise ValueError if `edges`, an array or Quantity, can't be the edges
    of histogram bins: they must be one-dimensional, at least two, finite and
    strictly increasing
    """
    values = np.asarray(getattr(edges, 'm', edges), dtype=float)
    if values.ndim != 1:
        raise ValueError("bin edges must be one-dimensional")
    if len(values) < 2:
        raise ValueError("there must be at least two bin edges")
    if not np.all(np.isfinite(values)):
        raise ValueError("bin edges must be finite")
    steps = np.diff(values)
    if np.any(steps <= 0):
        raise ValueError("bin edges are not increasing at index "
                         f"{int(np.argmax(steps <= 0)) + 1}")


def bins_match(a, b) -> bool:
    """ Whether the bin edges `a` are those of the reference `b`: the same
    number of them, with compatible units, each within 1e-6 of the smallest
    bin width of `b` of its counterpart
    """
    a, b = as_quantity(a), as_quantity(b)
    if a.shape != b.shape:
        return False
    try:
        converted = a.to(b.u).m
    except PintError:
        return False
    reference = np.asarray(b.m, dtype=float)
    steps = np.diff(reference)
    tolerance = 1e-6 * steps.min() if len(steps) and steps.min() > 0 else 0
    return bool(np.all(np.abs(converted - reference) <= tolerance))


class Histogram(object):
    """2-tuple mimicking np.histogram structure, with operator overloads
    so that bins are not added/scaled etc
    """
    # check only bins size when combining or total equality
    __strictchecking = False

    def __init__(self, hist, bin_edges=None):
        self.hist = hist
        self.bin_edges = bin_edges
        if bin_edges is None:
            self.bin_edges = np.arange(len(self.hist)+1)

    @classmethod
    def from_columns(cls, text: str, units=None, binsunit=None
                     ) -> 'Histogram':
        """ Parse a table with columns `low, high, value[, sigma[, sigmaup]]`
        separated by commas or whitespace. `sigma` is the (lower) uncertainty
        and `sigmaup` the upper one if asymmetric. Blank lines, `#` comments
        and a header line are skipped. Bins must be contiguous and increasing.
        Returns a Histogram of AsymmetricUncertainty quantities.
        """
        import pint
        from .asymmetric import AsymmetricUncertainty
        rows = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            fields = [f for f in line.replace(',', ' ').split()]
            try:
                row = [float(f) for f in fields]
            except ValueError:
                if not rows:
                    # header
                    continue
                raise ValueError(f"Line {lineno}: can't parse '{line}'")
            if not 3 <= len(row) <= 5:
                raise ValueError(f"Line {lineno}: expected 3 to 5 columns "
                                 f"(low, high, value[, sigma[, sigmaup]]), "
                                 f"got {len(row)}")
            if rows and len(row) != len(rows[0]):
                raise ValueError(f"Line {lineno}: expected {len(rows[0])} "
                                 f"columns, got {len(row)}")
            rows.append(row)
        if not rows:
            raise ValueError("No data found")
        data = np.array(rows).T
        low, high, value = data[:3]
        if np.any(high <= low):
            raise ValueError("Each bin's high edge must be above its low edge")
        if not np.allclose(high[:-1], low[1:]):
            raise ValueError("Bins must be contiguous: each bin's low edge "
                             "must equal the previous bin's high edge")
        edges = np.append(low, high[-1])
        sigma = data[3] if len(data) > 3 else np.zeros_like(value)
        sigmaup = data[4] if len(data) > 4 else None
        hist = pint.Quantity(AsymmetricUncertainty(value, sigma, sigmaup),
                             units)
        return cls(hist, pint.Quantity(edges, binsunit))

    @classmethod
    def from_dict(cls, d: dict, units=None, binsunit=None) -> 'Histogram':
        """ Read the HistogramField JSON shape: `value`, `bins` and optional
        `sigma`, `sigmaup`, `units`, `binsunit`. `units` and `binsunit`
        are defaults if not given in `d`
        """
        from .fields import HistogramField
        if not isinstance(d, dict) or 'value' not in d or 'bins' not in d:
            raise ValueError("Histogram JSON needs 'value' and 'bins' keys")
        d = {k: np.asarray(v, dtype=float) if isinstance(v, list) else v
             for k, v in d.items()}
        if 'sigma' not in d:
            d['sigma'] = np.zeros_like(d['value'])
        hist = HistogramField(units=units, binsunit=binsunit).to_python(d)
        if len(hist.hist) != len(hist.bin_edges) - 1:
            raise ValueError("Histogram needs one more bin edge than values")
        return hist

    @property
    def binwidths(self):
        return self.bin_edges[1:] - self.bin_edges[:-1]

    def find_bin(self, x):
        """Find the index of the bin where x is.
        Args:
            x (float): value to search for. must have same units as bins
        Returns:
            bin (int): -1 if before first bin, Nbins if outside last bin
        """
        return self.bin_edges.searchsorted(x, 'right')-1

    def val(self, x, interp=None):
        """Get the value of the histogram at x. x must be in the same
        units as the bins.
        Args:
            x (float): Value to test
            interp (str): Currently: if truthy, linearly interpolate the value
                          between bins. For future use: accept string
                          specifying interpolation method
        Returns:
            val (float): value of bin where x is, None if x is outside bins
        """
        bin = self.find_bin(x)
        if bin < 0 or bin >= len(self.hist):
            return None
        val = self.hist[bin]
        if interp and bin < len(self.hist)-1:
            slope = ((self.hist[bin + 1] - self.hist[bin]) /
                     (self.bin_edges[bin + 1] - self.bin_edges[bin]))
            val += slope * (x-self.bin_edges[bin])
        return val

    def _edges(self):
        """ The bin edges as a plain float array, and their unit or None """
        edges = self.bin_edges
        unit = getattr(edges, 'u', None)
        return np.asarray(edges.m if unit is not None else edges,
                          dtype=float), unit

    @staticmethod
    def _add_overlaps(edges, out, low, high, sign):
        """ Add `sign` times the length of the overlap of [low, high] with
        each bin to `out`, touching only the bins that overlap it
        """
        first = max(int(np.searchsorted(edges, low, 'right')) - 1, 0)
        last = min(int(np.searchsorted(edges, high, 'left')), len(edges) - 1)
        if last <= first:
            return
        seg = edges[first:last + 1]
        out[first:last] += sign * np.maximum(
            np.minimum(seg[1:], high) - np.maximum(seg[:-1], low), 0)

    def roi_weights(self, a=None, b=None, binwidth: bool = True,
                    average: bool = False, exclude=()) -> np.ndarray:
        """ The weights of the bins in the sum that integrates (or averages)
        the histogram from `a` to `b`, leaving out the `exclude` ranges. The
        result is a plain array with the length of the histogram, in the
        unit of the bin edges, to use with `apply_weights`.

        The weight of a bin is the length of it that is included, which is
        the fraction of the bin that is included times its width. If
        `binwidth` is False it is just the fraction. If `average`, the
        weights are then divided by the total included length, so they sum
        to 1 (or the total of the fractions, if not `binwidth`).

        Args:
            a, b: the limits, with the same dimensionality as the bin edges if
                  they have units. None uses the lowest or highest edge
            exclude: (low, high) pairs, in any order, which can overlap. The
                     parts outside [a, b] are ignored
        """
        edges, unit = self._edges()

        def magnitude(x):
            if hasattr(x, 'to'):
                return float(x.to('dimensionless' if unit is None else unit).m)
            return float(x)

        low = edges[0] if a is None else max(magnitude(a), edges[0])
        high = edges[-1] if b is None else min(magnitude(b), edges[-1])
        lengths = np.zeros(len(edges) - 1)
        if low < high:
            self._add_overlaps(edges, lengths, low, high, 1)
            for lo, hi in merge_ranges([(magnitude(x), magnitude(y))
                                        for x, y in exclude]):
                lo, hi = max(lo, low), min(hi, high)
                if lo < hi:
                    self._add_overlaps(edges, lengths, lo, hi, -1)
            np.maximum(lengths, 0, out=lengths)
        weights = lengths
        if not binwidth:
            widths = np.diff(edges)
            weights = np.divide(lengths, widths, out=np.zeros_like(lengths),
                                where=widths > 0)
        if average and (total := lengths.sum()) > 0:
            weights = weights / total
        return weights

    def apply_weights(self, weights, binwidth: bool = True,
                      average: bool = False):
        """ The sum of the histogram's values times `weights`, from
        `roi_weights` called with the same `binwidth` and `average`. The
        result has the unit of the values times that of the bin edges if
        `binwidth` and not `average`, divided by it if `average` and not
        `binwidth`, and the unit of the values otherwise. Raises ValueError if
        no bin has a weight. Only the bins from the first to the last with a
        weight are used.
        """
        support = np.flatnonzero(weights)
        if not len(support):
            raise ValueError("No bins are included")
        span = slice(support[0], support[-1] + 1)
        values = self.hist
        unit = getattr(values, 'u', None)
        if unit is not None:
            values = values.m
        # AsymmetricUncertainty.integrate handles the zeros within the span,
        # such as in the weights of excluded ranges, correctly
        result = values[span].dot(weights[span])
        edge_unit = getattr(self.bin_edges, 'u', None)
        power = int(binwidth) - int(average)
        if edge_unit is not None and power:
            unit = edge_unit ** power if unit is None else unit * edge_unit ** power
        if unit is None:
            return result
        return units.Quantity(result, unit)

    def integrate(self, a=None, b=None, binwidth=True, exclude=()):
        """Integrate the histogram from a to b. If a and b do not correspond to
        exact bin edges, the value in the bin will be interpolated.

        Args:
            a (float): low range. If bin_edges has units, this must have the
                       same dimensionality. if None, use low bound
            b (float): upper edge. if None, use upper bound
            binwidth (bool): if True (default), multiply each bin by the bin
                             width. If False, just add bin values
            exclude: (low, high) pairs to leave out of the range
        """
        return self.apply_weights(
            self.roi_weights(a, b, binwidth, False, exclude), binwidth, False)

    def average(self, a=None, b=None, binwidths=True, exclude=()):
        """Calculate the average from a to b. See `integrate` for description
        of the arguments
        """
        return self.apply_weights(
            self.roi_weights(a, b, binwidths, True, exclude), binwidths, True)

    @classmethod
    def sum_all(cls, hists) -> 'Histogram':
        """ The same as adding `hists` one by one, but the contents are added
        in one pass if they are (Quantities of) AsymmetricUncertainties
        """
        from .asymmetric import sum_values
        hists = list(hists)
        first = hists[0]
        for hist in hists[1:]:
            hist._testbins(first)
        return cls(sum_values(h.hist for h in hists), first.bin_edges)

    def __repr__(self):
        return f"Histogram<{len(self.hist)} bins>"

    def __str__(self):
        return repr(self)

    def _testbins(self, other):
        try:
            otherbins = other.bin_edges
        except AttributeError:
            return self.bin_edges
        if otherbins is None:
            return self.bin_edges
        elif self.bin_edges is None:
            return otherbins
        else:
            binsmatch = (self.bin_edges.shape == otherbins.shape)
            if self.__strictchecking:
                binsmatch = np.array_equal(self.bin_edges, otherbins)
            if not binsmatch:
                msg = ("Can't combins hists with different binning: %s and %s"
                       % (self.bin_edges, otherbins))
                raise ValueError(msg)
        return self.bin_edges

    def _combine(self, other, op, inplace=False):
        # treat None as zero
        if other is None:
            other = 0
        # make sure bins are equal
        bins = self._testbins(other)

        try:
            otherhist = other.hist
        except AttributeError:
            # how to tell the difference between a 2-tuple and 1D array???
            otherhist = other
        if inplace:
            self.hist = op(self.hist, otherhist)
            return self
        else:
            return self.__class__(op(self.hist, otherhist), bins)

    # TODO: should we provide for adding raw spectra rather than Histograms?
    # binary copy operators

    def __add__(self, other):
        return self._combine(other, operator.add)

    def __sub__(self, other):
        return self._combine(other, operator.sub)

    def __mul__(self, other):
        return self._combine(other, operator.mul)

    def __floordiv__(self, other):
        return self._combine(other, operator.floordiv)

    def __truediv__(self, other):
        return self._combine(other, operator.truediv)

    def __mod__(self, other):
        return self._combine(other, operator.mod)

    def __pow__(self, other):
        return self._combine(other, operator.pow)

    # do we need logical/bitwise operators??

    # binary in-place operators
    def __iadd__(self, other):
        return self._combine(other, operator.add, inplace=True)

    def __isub__(self, other):
        return self._combine(other, operator.sub, inplace=True)

    def __imul__(self, other):
        return self._combine(other, operator.mul, inplace=True)

    def __ifloordiv__(self, other):
        return self._combine(other, operator.floordiv, inplace=True)

    def __itruediv__(self, other):
        return self._combine(other, operator.truediv, inplace=True)

    def __imod__(self, other):
        return self._combine(other, operator.mod, inplace=True)

    def __ipow__(self, other):
        return self._combine(other, operator.pow, inplace=True)

    # reverse binary operators
    # these should only ever be called if type(other) != type(self)
    def __radd__(self, other):
        return self.__class__(other + self.hist, self.bin_edges)

    def __rsub__(self, other):
        return self.__class__(other - self.hist, self.bin_edges)

    def __rmul__(self, other):
        return self.__class__(other * self.hist, self.bin_edges)

    def __rfloordiv__(self, other):
        return self.__class__(other // self.hist, self.bin_edges)

    def __rtruediv__(self, other):
        return self.__class__(other / self.hist, self.bin_edges)

    def __rmod__(self, other):
        return self.__class__(other % self.hist, self.bin_edges)

    def __rpow__(self, other):
        return self.__class__(other ** self.hist, self.bin_edges)

    # unary operators
    def __neg__(self):
        return self.__class__(-self.hist, self.bin_edges)

    def __abs__(self):
        return self.__class__(operator.abs(self.hist), self.bin_edges)
