""" The results of a component's SourceTerms, as a table to sum over.

Each SourceTerm's result for a scalar or spectrum is its emission rate times
the value of a hit efficiency, summed over its hit efficiencies. The rates and
the hit efficiencies are each shared by many SourceTerms, so rather than
keeping a value for every SourceTerm and name, the table keeps the SourceTerms'
weights on the (independent) rates and hit efficiencies, and sums any subset
of them in one pass, with the correlations accounted for (see `bilinear`).
"""
from collections import defaultdict
from typing import Dict, List, NamedTuple, Optional, Tuple
import numpy as np
from bson import ObjectId
from ..asymmetric import AsymmetricUncertainty
from ..bilinear import bilinear_each, bilinear_sum, bilinear_value
from ..common import units as unitreg
from ..histogram import Histogram
from ..termcalc import HitBlock, RateTerm

# SourceTerms at a time when finding the size of each one's spectra
_SIZE_CHUNK = 500
# a guess of the bytes of memory used by the Python objects of a SourceTerm
_TERM_BYTES = 600


class TermRecord(NamedTuple):
    """ One SourceTerm: its key for each groupby (see
    `SourceTerm.group_keys`), the confidence rating of the component it
    belongs to, the terms of its rate, and its hit efficiencies' blocks.
    Component keys are the full placement paths
    """
    sourceterm_id: ObjectId
    keys: dict
    rating: Optional[int]
    rate: Tuple[RateTerm, ...]
    blocks: Tuple[HitBlock, ...]


class _Columns:
    """ The hit efficiency variables for one name: the index of the column of
    each block (-1 if it has none) with its scale, and the columns' moments
    """
    def __init__(self, blocks: List[HitBlock], name: str):
        index = {}
        self.keys = []
        modes, v0s, v1s = [], [], []
        self.block_col = np.full(len(blocks), -1, dtype=np.int64)
        self.block_scale = np.ones(len(blocks))
        self.display = self.bin_edges = None
        for b, block in enumerate(blocks):
            hit = block.values.get(name)
            if hit is None:
                continue
            col = index.get(hit.key)
            if col is None:
                col = index[hit.key] = len(modes)
                self.keys.append(hit.key)
                mode, v0, v1 = hit.moments
                modes.append(mode)
                v0s.append(v0)
                v1s.append(v1)
                if self.bin_edges is None:
                    self.bin_edges, self.display = hit.bin_edges, hit.display
            self.block_col[b] = col
            self.block_scale[b] = hit.scale
        if len({np.shape(m) for m in modes}) > 1:
            raise ValueError(f"Can't combine {name} with different binning")
        self.moments = tuple(np.array(a, dtype=float)
                             for a in (modes, v0s, v1s))
        self.nbytes = sum(a.nbytes for a in self.moments)
        # the sum of a bin's width times its mean is in units of this
        self._unit = (None if self.display is None else float(
            unitreg.Quantity(1., self.display).to_reduced_units().m))


class TermTable:
    """ A table of the SourceTerms in `records`, which are numbered in order.
    Those without results (no hit efficiency has anything for them) are left
    out. `component_path` converts a record's component key to the path
    below the object whose budget is being made. Read-only once made: shared
    by all the budgets of the object, so safe to use from several threads
    """
    def __init__(self, records, component_path):
        self.ids: List[ObjectId] = []
        self.keys: List[dict] = []
        self.ratings: List[Optional[int]] = []
        rate_index = {None: 0}
        self._rate_keys = [None]
        rate_moments = [(1., 0., 0.)]
        block_index = {}
        self._blocks: List[HitBlock] = []
        ptr = [0]
        rows, coefficients, blocks = [], [], []
        for record in records:
            used = [block for block in record.blocks if block.values]
            if not used:
                continue
            self.ids.append(record.sourceterm_id)
            self.keys.append(dict(
                record.keys,
                component=component_path(record.keys['component'])))
            self.ratings.append(record.rating)
            block_ids = []
            for block in used:
                b = block_index.get(block.key)
                if b is None:
                    b = block_index[block.key] = len(self._blocks)
                    self._blocks.append(block)
                block_ids.append(b)
            for term in record.rate:
                row = rate_index.get(term.key)
                if row is None:
                    row = rate_index[term.key] = len(rate_moments)
                    self._rate_keys.append(term.key)
                    rate_moments.append(tuple(float(x)
                                              for x in term.moments))
                for b in block_ids:
                    rows.append(row)
                    coefficients.append(term.coefficient)
                    blocks.append(b)
            ptr.append(len(rows))
        # the blocks are shared by many terms, with the spectra if any
        self._block_bytes = sum(
            sum(np.asarray(a).nbytes for a in hit.moments)
            for block in self._blocks
            for hit in block.values.values())
        self._ptr = np.array(ptr, dtype=np.int64)
        self._rows = np.array(rows, dtype=np.int64)
        self._coefficients = np.array(coefficients)
        self._block_of = np.array(blocks, dtype=np.int64)
        self._term_of = np.repeat(np.arange(len(self.ids)),
                                  np.diff(self._ptr))
        self._rate = tuple(np.array(a) for a in zip(*rate_moments))
        self._columns: Dict[str, _Columns] = {}
        self._sizes: Dict[str, np.ndarray] = {}
        self._having: Dict[str, np.ndarray] = {}

    def __len__(self) -> int:
        return len(self.ids)

    @property
    def nbytes(self) -> int:
        """ An estimate of the memory used """
        arrays = (self._ptr, self._rows, self._coefficients, self._block_of,
                  self._term_of, *self._rate)
        extra = list(self._columns.values())
        return (len(self) * _TERM_BYTES + self._block_bytes +
                sum(a.nbytes for a in arrays) +
                sum(c.nbytes for c in extra) +
                sum(a.nbytes for a in list(self._sizes.values())))

    def columns(self, name: str) -> _Columns:
        try:
            return self._columns[name]
        except KeyError:
            return self._columns.setdefault(name, _Columns(self._blocks, name))

    def having(self, name: str) -> np.ndarray:
        """ Boolean array: which terms have a result for `name` """
        try:
            return self._having[name]
        except KeyError:
            pass
        found = self.columns(name).block_col[self._block_of] >= 0
        result = np.bincount(self._term_of, weights=found,
                             minlength=len(self)) > 0
        return self._having.setdefault(name, result)

    def _entries(self, name: str, idx, scale: float = 1.):
        """ The (term, rate row, column, weight) of the entries for `name`
        of the terms `idx`, as arrays; terms are numbered by position in `idx`
        """
        idx = np.asarray(idx, dtype=np.int64)
        starts = self._ptr[idx]
        counts = self._ptr[idx + 1] - starts
        ends = np.cumsum(counts)
        entry = (np.repeat(starts - ends + counts, counts)
                 + np.arange(ends[-1] if len(ends) else 0))
        columns = self.columns(name)
        block = self._block_of[entry]
        col = columns.block_col[block]
        keep = col >= 0
        weights = (self._coefficients[entry] * columns.block_scale[block]
                   * scale)
        term = np.repeat(np.arange(len(idx)), counts)
        return (term[keep], self._rows[entry][keep], col[keep], weights[keep],
                columns)

    def total(self, name: str, idx, scale: float = 1.
              ) -> Optional[AsymmetricUncertainty]:
        """ The sum of scalar `name` of the terms numbered `idx`, times
        `scale`, or None if none have a result
        """
        _, rows, cols, weights, columns = self._entries(name, idx, scale)
        if not len(rows):
            return None
        return bilinear_value(
            rows, cols, weights, self._rate, columns.moments,
            leaves=lambda r, c, w: self._leaves(columns, r, c, w))

    def _leaves(self, columns: _Columns, rows, cols, weights) -> dict:
        """ The coefficients of the expression of a sum: the rate and
        hit efficiency variables of the entries (as leaves with ids from their
        keys) in sets, with their summed weights. So that the sums of the
        same table that share any variables are correlated like the variables
        """
        def leaf(kind, key, moments, i):
            return AsymmetricUncertainty(
                float(moments[0][i]), float(np.sqrt(moments[1][i])),
                float(np.sqrt(moments[2][i])), id=f"{kind}:{key}")
        rate_leaves = {r: None if r == 0 else
                       leaf('rate', self._rate_keys[r], self._rate, r)
                       for r in np.unique(rows)}
        hit_leaves = {c: leaf('hit', columns.keys[c], columns.moments, c)
                      for c in np.unique(cols)}
        coefficients = defaultdict(float)
        for r, c, w in zip(rows.tolist(), cols.tolist(), weights.tolist()):
            variables = {hit_leaves[c]}
            if rate_leaves[r] is not None:
                variables.add(rate_leaves[r])
            coefficients[frozenset(variables)] += w
        return coefficients

    def total_spectrum(self, name: str, idx) -> Optional[Histogram]:
        """ The sum of spectrum `name` of the terms numbered `idx`, or None
        if none have a result
        """
        _, rows, cols, weights, columns = self._entries(name, idx)
        if not len(rows):
            return None
        mode, s0, s1 = bilinear_sum(rows, cols, weights, self._rate,
                                    columns.moments)
        return Histogram(unitreg.Quantity(
            AsymmetricUncertainty(mode, s0, s1), columns.display),
            columns.bin_edges)

    def each(self, name: str, idx) -> tuple:
        """ The mode and lower and upper standard deviation of scalar or
        spectrum `name` of each of the terms numbered `idx` (zero where they
        have no result), as arrays by position in `idx`
        """
        term, rows, cols, weights, columns = self._entries(name, idx)
        return bilinear_each(term, len(idx), rows, cols, weights, self._rate,
                             columns.moments)

    def sizes(self, name: str) -> np.ndarray:
        """ How big each term's result for `name` is, in its display unit, to
        rank and weight them by: for a scalar the upper limit of limits, or
        else the central value. For a spectrum the integral of its
        approximate mean, treating each bin as a split normal, which unlike
        the most likely value adds up and is positive for upper limits. Zero
        where there is no result
        """
        try:
            return self._sizes[name]
        except KeyError:
            pass
        columns = self.columns(name)
        have = np.flatnonzero(self.having(name))
        result = np.zeros(len(self))
        for i in range(0, len(have), _SIZE_CHUNK):
            chunk = have[i:i + _SIZE_CHUNK]
            mode, s0, s1 = self.each(name, chunk)
            if columns.display is None:
                limit = (s1 != 0) & (mode == 0) & (s0 == 0)
                if limit.any():
                    upper = AsymmetricUncertainty(
                        mode, s0, s1).get_upper_limit(0.9)
                    mode = np.where(limit, upper, mode)
                result[chunk] = mode
            else:
                widths = np.diff(np.asarray(getattr(
                    columns.bin_edges, 'm', columns.bin_edges), dtype=float))
                mean = mode + np.sqrt(2 / np.pi) * (s1 - s0)
                result[chunk] = (np.nansum(mean * widths, axis=1)
                                 * columns._unit)
        return self._sizes.setdefault(name, result)
