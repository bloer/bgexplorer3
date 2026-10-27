""" Break down a component's background budget by child component, isotope,
material or source category, like the summary plots in e.g. the nEXO
sensitivity papers. The breakdowns can be filtered to drill down into the
model, crossfilter style.

Every SourceTerm's results are in the version's display units (see
`termcalc`), so the breakdowns are sums of plain values.
"""
from .filters import GROUPBY, BudgetFilter
from .terms import Term, available_scalars, term_values
from .grouping import COMPONENT_DEPTH, cached_filtered, filtered_cached
from .dashboard import dashboard
from .table import table, table_columns
from .spectra import spectrum_breakdown, spectrum_names

__all__ = ['GROUPBY', 'BudgetFilter', 'Term', 'available_scalars',
           'term_values', 'COMPONENT_DEPTH', 'cached_filtered',
           'filtered_cached', 'dashboard', 'table', 'table_columns',
           'spectrum_breakdown', 'spectrum_names']
