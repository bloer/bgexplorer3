from scipy.stats import norm, halfnorm, rv_continuous
import numpy as np
import operator
import io
from typing import Union, Optional, Tuple
NumOrArray = Union[int, float, np.ndarray]
try:
    import pint
except ImportError:
    pint = None

class AsymmetricErrorDistribution(rv_continuous):
    """ Scipy rv_continuous defined by AsymmetricError data """
    def __init__(self, data: 'AsymmetricError'):
        self.data = data
        super().__init__(name='AsymmetricError')

    @property
    def mode(self):
        return self.data.mode

    @property
    def s0(self):
        return self.data.s0

    @property
    def s1(self):
        return self.data.s1

    @property
    def qlow(self):
        return self.data.qlow

    def _sigma(self, x):
        """ sigma in the neighborhood of x """
        return np.where(x < self.mode, self.s0, self.s1)

    def _pdf(self, x):
        if self.s0 <= 0:
            return halfnorm(self.mode, self.s1).pdf(x)
        prefix = np.sqrt(2/np.pi)/(self.s0 + self.s1)
        experand = -(x-self.mode)**2/(2*self._sigma(x)**2)
        return prefix*np.exp(experand)

    def _ppf(self, q):
        if self.s0 <= 0:
            return halfnorm(self.mode, self.s1).ppf(q)
        qpup = (1+q-2*self.qlow)/(2*(1-self.qlow))
        return np.where(q < self.qlow, norm(self.mode, self.s0).ppf(q/self.qlow/2),
                        norm(self.mode, self.s1).ppf(qpup))

    def _cdf(self, x):
        if self.s0 <= 0:
            return halfnorm(self.mode, self.s1).cdf(x)
        return np.where(x < self.mode, norm(self.mode, self.s0).cdf(x)*self.qlow*2,
                        norm(self.mode, self.s1).cdf(x)*(1-self.qlow)*2 + 2*self.qlow-1)


class AsymmetricError:
    """ This class is designed for combining upper limits and regular
    measurements in a logically consistent way, though we have to fudge the
    statistics a bit.

    This class aims to solve two contradictory requirements when combining
    measurements and pper limits:
    1) The sum of multiple upper limits should be an upper limit
    2) The sum of an upper limit and a measurement should not increase the
       measurement's lower bound.

    The reason (1) is desirable is hopefully obvious. If nothing else, *how*
    the sum of multiple limits would turn into a measurement would depend
    strongly on the particular choice of prior. The need for (2) is best shown
    by example.  Suppose we have a very precise measurement 10+/-0.1 and an
    upper limit represented by 1+/-5. The naive sum of these would be ~11+/-5.

    And here's why these two desires are incompatible. (2) requires that our
    representation of upper limits (and technically any measurement) to be
    strictly greater than 0 (ot at least have a small enough fraction of the
    probability extending below 0 that we're comfortable neglecting it). But,
    the addition (i.e., convoution) of multiple positive-definite PDFs will
    eventually produce a result incompatible with 0, i.e., no longer an upper
    limit!

    Because we *can't* meet these two criteria with a proper statistical
    represeatation, we are going to fudge some things. The AsymmetricError
    is represented by a mode and upper and lower standard deviation. Math
    operations treat the two sigmas identically , i.e.
    if z = f(x,y), var_z = (df/dx)^2 var_x + (df/dy)^2 var_y, where var = sigma**2
    An upper limit would be represented as 0+s1-0, where s1 is determined from
    the provided confidence limit. If a measurement with significant probability
    less than zero is provided, the lower uncertainty is set to the mode. I.e.
    1+/-5 is converted to 1+5-1. The minimum value can be set with the
    `setminlowerz` classmethod, i.e., if set to 2, 1+/-5 would become 1+5-0.5
    The addition of two upper limits yields another upper limit:
    0+x-0 + 0+y-0 = 0+sqrt(x**2+y**2)-0
    In our example above,
    10+/-0.1 + 1+5-1 = 11+5-1. The uncertainties add as though independent
    gaussians, and the modes behave as expected.

    The class also has methods to get the pdf and ppf (quantile). These are
    calculated assuming a piecewise distribution of the form
    sqrt(2/pi) 1/(s0+s1) exp(-(x-mode)**2/(2*sigma**2),
        where sigma = s0 if x < mode else s1
    It's important to stress that this PDF definition is NOT compatible with the
    defined behavior for adding these objects!
    """
    __slots__ = ('mode', 's0', 's1', '_v0', '_v1', '_modesq')
    _minmeanz = 1
    @classmethod
    def setminlowerz(cls, zmin):
        cls._minmeanz = zmin

    def __init__(self, value: NumOrArray, sigma: NumOrArray,
                 sigmaup: Optional[NumOrArray] = None,
                 forceposdef: bool = False):
        """ Constructor. For symmetric errors, supply the mean and
        standard deviation. For asymmetric errors, supply the mode,
        lower sigma, and upper sigma.
        if `forceposdef` is True and the mode is less than `_minmeanz`
        times the lower sigma, lower sigma will be adjusted.
        """
        self.mode = value
        self.s0 = sigma
        self.s1 = sigmaup if sigmaup is not None else self.s0
        if not np.isscalar(self.mode):
            self.mode = np.asarray(self.mode)
            self.s0 = np.asarray(self.s0)
            self.s1 = np.asarray(self.s1)
        self._v0 = None
        self._v1 = None
        self._modesq = None

        if (forceposdef and np.isscalar(self.mode) and
                self.mode < self.s0*self._minmeanz):
            self.s0 = self.mode / self._minmeanz

    # todo: is it more efficient to store variance rather than std?
    @property
    def v0(self):
        if self._v0 is None:
            self._v0 = self.s0**2
        return self._v0

    @property
    def v1(self):
        if self._v1 is None:
            self._v1 = self.s1**2
        return self._v1

    @property
    def modesq(self):
        if self._modesq is None:
            self._modesq = self.mode**2
        return self._modesq

    def serialize(self, compressarrays: bool = True) -> Tuple:
        """ Convert to a tuple for storage """
        result = (self.mode, self.s0, self.s1)
        if np.all(self.s0 == self.s1):
            result = (self.mode, self.s0)
        if compressarrays and isinstance(result[0], np.ndarray):
            # compress the array as a binary blob
            buf = io.BytesIO()
            np.savez_compressed(buf, *result)
            result = (buf.getvalue(),)
        return result

    @staticmethod
    def serializeq(asym: 'AsymmetricErrors', compressarrays: bool = True) -> Tuple:
        """ Serialize a Quantity with units """
        result = asym.serialize(compressarrays=compressarrays)
        if hasattr(asym, 'units') and str(asym.units) != 'dimensionless':
            result = result + (str(asym.units),)
        return result

    @classmethod
    def deserialize(cls, val: Tuple, unit_registry = None,
                    force_quantity: bool = False) -> 'AsymmetricError':
        """ Construct an AsymmetricError from it's serialized represeation
        If the serialized object contained a unit, unit_registry must be a
        `pint.UnitRegistry`
        """
        unit = None
        if isinstance(val[-1], str) or force_quantity:
            if unit_registry is None:
                if pint is None:
                    raise ValueError(
                        "Can't deserialize unitted values with UnitRegistry")
                else:
                    unit_registry = pint.get_application_registry()
            unit = 'dimensionless'
            if isinstance(val[-1], str):
                unit = val[-1]
                val = val[:-1]
        if isinstance(val[0], bytes) and len(val) == 1:
            # this is a compressed numpy archive
            val = tuple(np.load(io.BytesIO(val[0])).values())
        result = AsymmetricError(*val, forceposdef=False)
        if unit is not None:
            result = unit_registry.Quantity(result, unit)
        return result

    @classmethod
    def fromlimit(cls, limit: float, quantile: float = 0.9) -> 'AsymmetricError':
        z = norm.isf((1.-quantile)/2.)
        sigmaup = limit / z
        return cls(0, 0, sigmaup)

    @classmethod
    def fromstring(cls, val: str) -> 'AsymmetricError':
        if val.startswith('<'):
            return cls.fromlimit(float(val[1:]))
        mode, e1 = val.split('+')
        s1, s0 = val.split('-')
        s1 = float(s1) if (s1 and s1 != '/') else None
        return cls(float(mode), float(s0), s1)

    @classmethod
    def _fromcounts_scalar(cls, counts: float, forceposdef: bool) -> 'AsymmetricError':
        s0 = np.sqrt(counts)
        s1 = s0 if counts > 0 else 1.3983007
        return cls(counts, s0, s1, forceposdef)

    @classmethod
    def _fromcounts_array(cls, counts: np.ndarray, forceposdef: bool) -> 'AsymmetricError':
        s0 = np.sqrt(counts)
        s1 = np.copy(s0)
        s1[s1 == 0] = 1.3983007
        if forceposdef:
            bad = (counts < (s0 * cls._minmeanz))
            s0[bad] = counts[bad]/cls._minmeanz
        return cls(counts, s0, s1)

    @classmethod
    def fromcounts(cls, counts: NumOrArray, forceposdef: bool = False) -> 'AsymmetricError':
        """ convert integer counts """
        if np.isscalar(counts):
            return cls._fromcounts_scalar(counts, forceposdef)
        else:
            return cls._fromcounts_array(counts, forceposdef)

    def isupperlimit(self, strict: bool = True) -> bool:
        if not np.isscalar(self.mode):
            return None
        if strict:
            return (self.mode == 0 and self.s0 == 0)
        return self.mode <= self.s0 * self._minmeanz

    def islowerlimit(self, strict: bool = True) -> bool:
        """ A positive-definite lower limit implies the mode is infinite
        `strict` is not used at this time but included to keep the call sig
        the same as `isupperlimit`
        """
        if not np.isscalar(self.mode):
            return None
        return self.mode == np.inf and self.s0 > 0

    @property
    def qlow(self):
        """ Fraction of probability in lower half, equivalent to the cdf
        at mode """
        return self.s0 / (self.s0 + self.s1)

    def alpha(self, q):
        """ Effective alpha parameter for a given quantile """
        return np.where(q < self.qlow, q / self.qlow, (1.-q)/(1.-self.qlow))

    def ppf(self, q):
        return AsymmetricErrorDistribution(self).ppf(q)

    def __format__(self, format_spec):
        format_spec = ''.join(['{:', format_spec, '}'])
        if self.isupperlimit():
            return ''.join(['<', format_spec.format(self.ppf(0.9))])
        s = tuple(format_spec.format(x)
                  for x in self.serialize(compressarrays=False))
        if len(s) == 2:
            return f"{s[0]}+/-{s[1]}"
        elif len(s) == 3:
            return f"{s[0]}+{s[2]}-{s[1]}"

    def __str__(self):
        return "{}".format(self)

    def __repr__(self):
        return f"AsymmetricError{self.serialize(compressarrays=False)}"

    # Operator overloads
    # TODO: combine the redundancies here
    # TODO: implement iadd, isub, etc.

    def rezero(self, inplace=False):
        """ Zero the sigmas of all entries with zero mode.  See `addtreatzero`
        for a description of why you'd use this.
        """
        if not inplace:
            result = AsymmetricError(self.mode, self.s0, self.s1)
            if not np.isscalar(self.s1):
                result.s1 = self.s1.copy()
            result.rezero(inplace=True)
            return result

        isupper = self.isupperlimit(strict=True)
        self._v1 = None
        if isupper:
            self.s1 = 0
        elif isupper is None:
            self.s1[self.mode == 0] = 0
        return self

    def addtreatzero(self, other: 'AsymmetricError'):
        """ In some cases, such as rebinning a histogram, upper limits should
        not combine. E.g., if I have an AsymmetricError representing a histogram
        of counts and want to integrate to get the total number of counts,
        any bins with zero entries should not contribute to the total error.
        In that case, use this method to perform the addition properly

        Rules:
            x  + UL = x
            UL + x  = x
            UL + UL = UL
        """
        if not isinstance(other, AsymmetricError):
            return self + other
        result = self.rezero() + other.rezero()
        if np.isscalar(self.mode):
            if self.mode == 0 and result.s1 == 0:
                result.s1 = max(self.s1, other.s1)
        else:
            tofix = (result.mode == 0) & (result.s1 == 0)
            result.s1[tofix] = np.max([self.s1, other.s1], axis=0)[tofix]
        return result

    def average(self, other, weight1=1, weight2=1):
        return (self*weight1).addtreatzero(other*weight2)/(weight1 + weight2)

    def __neg__(self):
        return AsymmetricError(-self.mode, self.s1, self.s0)

    def __add__(self, other):
        try:
            return AsymmetricError(self.mode + other.mode,
                                   np.sqrt(self.v0 + other.v0),
                                   np.sqrt(self.v1 + other.v1))
        except AttributeError:
            return AsymmetricError(self.mode + other, self.s0, self.s1)

    def __sub__(self, other):
        try:
            return AsymmetricError(self.mode - other.mode,
                                   np.sqrt(self.v0 + other.v1),
                                   np.sqrt(self.v1 + other.v0))
        except AttributeError:
            return AsymmetricError(self.mode - other, self.s0, self.s1)

    def __mul__(self, other):
        try:
            return AsymmetricError(self.mode * other.mode,
                                   np.sqrt(self.v0*other.modesq +
                                           other.v0*self.modesq +
                                           self.v0 * other.v0),
                                   np.sqrt(self.v1*other.modesq +
                                           other.v1*self.modesq +
                                           self.v1 + other.v1))
        except AttributeError:
            pass
        # test for pint Quantities
        if hasattr(other, 'dimensionality'):
            other = 1*other  # ensure it's a quantity
            return other.__class__(self*other.m, other.u)
        return AsymmetricError(self.mode * other, self.s0*other, self.s1*other)

    # todo: not clear this is being done correctly.
    def __truediv__(self, other):
        try:
            return AsymmetricError(self.mode / other.mode,
                                   np.sqrt(self.v0/other.modesq +
                                           other.v1*self.modesq/other.modesq**2 +
                                           other.v1**2 * self.modesq/other.modesq**3 +
                                           self.v0*other.v1/other.modesq**2),
                                   np.sqrt(self.v1/other.modesq +
                                           other.v0*self.modesq/other.modesq**2 +
                                           other.v0**2 * self.modesq/other.modesq**3 +
                                           self.v1*other.v0/other.modesq**2))
        except AttributeError:
            return AsymmetricError(self.mode / other, self.s0/other, self.s1/other)

    def __radd__(self, other):
        return self + other

    def __rsub__(self, other):
        # if isinstance(other, AsymmetricError), then other.__add__ will be called
        return AsymmetricError(other-self.mode, self.s1, self.s0)

    def __rmul__(self, other):
        return self * other

    def __rtruediv__(self, other):
        if self.isupperlimit():
            return AsymmetricError(np.inf, other/self.s1, 0)
        return AsymmetricError(other / self.mode, other*self.s1/self.modesq,
                               other*self.s0/self.modesq)

    # NB: comparison operators test ONLY the mean, which is not great
    def _compare(self, other, op):
        try:
            return op(self.mode, other.mode)
        except AttributeError:
            return op(self.mode, other)

    def __eq__(self, other):
        return self._compare(other, operator.eq)

    def __ne__(self, other):
        return self._compare(other, operator.ne)

    def __lt__(self, other):
        return self._compare(other, operator.lt)

    def __le__(self, other):
        return self._compare(other, operator.le)

    def __gt__(self, other):
        return self._compare(other, operator.gt)

    def __ge__(self, other):
        return self._compare(other, operator.ge)

    def __bool__(self):
        return bool(self.mode)

    def __hash__(self):
        return id(self)

    # array-specific stuff
    def __len__(self):
        return len(self.mode)

    #@property
    #def __array_interface__(self):
        # todo: handle scalar case
    #    return self.mode.__array_interface__

    def __getitem__(self, *args, **kwargs):
        return AsymmetricError(self.mode.__getitem__(*args, **kwargs),
                               self.s0.__getitem__(*args, **kwargs),
                               self.s1.__getitem__(*args, **kwargs))

    def integrate(self, weights=None):
        # sum out entries with weights, applying zeroed ULs for combining
        weighted = self if weights is None else self*weights
        if np.isscalar(self.mode):
            return weighted

        weighted = weighted.rezero()
        result = AsymmetricError(np.sum(weighted.mode),
                                 np.sqrt(np.sum(weighted.v0)),
                                 np.sqrt(np.sum(weighted.v1)))

        # todo: should we check for all s1's to be equal? not sensible state otherwsie
        if result.mode == 0:
            if weights is None or np.isscalar(weights):
                # self.mode is all zeros
                result.s1 = np.max(weighted.s1)
            elif np.sum(weights) > 0:
                # take the error at the maximum weight as the integral
                result.s1 = weighted.s1[np.argmax[weights]]

        return result

    # def __array__ufunc__(ufunc, method, *inputs, **kwargs):

    def __array_function__(self, func, types, args, kwargs):
        if func not in (np.sum, np.dot):
            return NotImplemented
        if func is np.sum:
            return self.integrate()
        elif func is np.dot:
            weights = args[1] if args[0] is self else args[0]
            return self.integrate(weights)
