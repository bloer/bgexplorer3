""" The bill of materials of a component: a row for every assembly path below
it, with what each part is, how many there are, where its radioactivity
comes from, and how much it contributes to every scalar and ROI.

Rows are keyed by `RowKey` and their cells hold data only (text, numbers,
Quantities and `Link`s), so that two bills of materials can be compared cell
by cell, and the view decides how to show them.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from ..assay import Assay
from ..component import Assembly, Component
from ..cosmogenic import CosmogenicActivation
from ..emissionspec import Multiplier
from ..exposure import RadonExposure
from ..isotope import compare_source_names
from ..settings import get_settings
from ..sourceterm import SourceTerm
from .filters import BudgetFilter
from .grouping import Output, cached_filtered
from .table import path_sums, path_values, table_columns
from .terms import term_table

# the isotopes with their own columns
BULK_ISOTOPES = ('U238', 'Th232', 'K40')


class RowKey(NamedTuple):
    """ What identifies a row: the placement ids (as strings) from the root
    to the row's component, and that component's `original_id`
    """
    placement_ids: Tuple[str, ...]
    component: str


class Link(NamedTuple):
    """ Text to show, linked to the page `view` (default the document's own)
    of the document `endpoint` (a blueprint name, e.g. 'component') with
    `original_id` `id`, shown relative to the component `relativeto` if
    set, or to the external `url`, or to nothing
    """
    text: str
    endpoint: Optional[str] = None
    id: Optional[str] = None
    url: Optional[str] = None
    view: str = 'view'
    relativeto: Optional[str] = None


class BomColumn(NamedTuple):
    key: str
    label: str
    group: str
    visible: bool = True
    units: Optional[str] = None


@dataclass(frozen=True)
class BomRow:
    """ A cell is a string, a number, a Quantity, None, or a tuple of Links """
    key: RowKey
    outline: str
    depth: int
    has_children: bool
    cells: Dict[str, Any]


@dataclass(frozen=True)
class Bom:
    columns: Tuple[BomColumn, ...]
    rows: Tuple[BomRow, ...]


_BEFORE = (
    BomColumn('outline', 'Outline', 'identity'),
    BomColumn('name', 'Name', 'identity'),
    BomColumn('component_id', 'Component ID', 'identity', False),
    BomColumn('placement_id', 'Placement ID', 'identity', False),
    BomColumn('description', 'Description', 'identity'),
    BomColumn('weight', 'Wt (parent)', 'quantity'),
    BomColumn('total_weight', 'Total wt', 'quantity'),
    BomColumn('material', 'Material', 'quantity'),
    BomColumn('mass', 'Mass', 'quantity', True, 'kg'),
    BomColumn('surface_area', 'Surface area', 'quantity', False, 'm**2'),
    BomColumn('part', 'Part', 'identity'),
    BomColumn('owner', 'Owner', 'identity', False),
    BomColumn('comment', 'Comment', 'identity', False),
    BomColumn('confidence_rating', 'Confidence', 'identity', False),
    BomColumn('location', 'Location', 'hiteff', False),
    BomColumn('assay', 'Assay', 'assay'),
    BomColumn('assay_detail', 'Assay detail', 'assay', False),
) + tuple(BomColumn(iso, iso, 'isotopes') for iso in BULK_ISOTOPES) + (
    BomColumn('other_sources', 'Other sources', 'isotopes', False),
    BomColumn('cosmogenic', 'Cosmogenic', 'exposure'),
    BomColumn('radon', 'Radon', 'exposure'),
    BomColumn('sourceterms', 'Source Terms', 'sourceterms'),
)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _number(quantity, unit: str) -> Optional[float]:
    if quantity is None:
        return None
    return float(quantity.to(unit).m)


def _text(values) -> Optional[str]:
    return '; '.join(values) or None


def _part(info) -> tuple:
    if info is None:
        return ()
    text = ' / '.join(x for x in (info.vendor, info.partnum) if x)
    if not text:
        return ()
    return (Link(text, url=info.link or None),)


def _assay_text(assay: Assay) -> str:
    publication = assay.publication
    label = publication and (publication.shortlabel or publication.reference)
    return f"{assay.name} ({label})" if label else assay.name


def _assay_detail(assay: Assay) -> str:
    sample, measurement = assay.sample, assay.measurement
    details = [
        sample and (sample.name or sample.id) and
        f"sample {sample.name or sample.id}",
        measurement and (measurement.id or measurement.technique) and
        f"measurement {measurement.id or measurement.technique}",
        assay.radiopurityid and f"radiopurity.org {assay.radiopurityid}"]
    return f"{assay.name}: {', '.join(d for d in details if d)}"


def _bulk(specs) -> Tuple[Dict[str, str], Optional[str]]:
    """ The bulk rates of the isotopes with their own columns, and a summary
    of any others
    """
    named: Dict[str, List[str]] = {iso: [] for iso in BULK_ISOTOPES}
    other: List[str] = []
    for spec in specs:
        if spec.COMPUTED_SOURCES:
            continue
        for source in spec.sources:
            if source.multiplier is not Multiplier.mass:
                continue
            for iso in BULK_ISOTOPES:
                if compare_source_names(source.name, iso):
                    named[iso].append(str(source.rate))
                    break
            else:
                other.append(f"{source.name}: {source.rate}")
    return {iso: _text(rates) for iso, rates in named.items()}, _text(other)


def _total_duration(periods):
    return sum((p.duration for p in periods), 0 * periods[0].duration.u)


def _cosmogenic(specs) -> Optional[str]:
    return _text(
        f"{spec.material.name}: {_plural(len(spec.periods), 'period')}, "
        f"{_total_duration(spec.periods):~.3g}"
        for spec in specs
        if isinstance(spec, CosmogenicActivation) and spec.periods)


def _radon(specs) -> Optional[str]:
    return _text(
        f"{_plural(len(spec.periods), 'period')}, "
        f"{_total_duration(spec.periods):~.3g}; Pb210 {spec.pb210_rate():~.3g}"
        for spec in specs
        if isinstance(spec, RadonExposure) and spec.periods)


def _component_cells(comp: Component) -> dict:
    """ The cells that depend on the component only """
    specs = comp.specs
    assays = [spec for spec in specs if isinstance(spec, Assay)]
    bulk, other = _bulk(specs)
    info = comp.purchaseinfo
    cells = dict(
        component_id=str(comp.original_id),
        description=comp.description or None,
        material=comp.material or None,
        mass=_number(comp.mass, 'kg'),
        surface_area=_number(comp.surface_area, 'm**2'),
        part=_part(info),
        owner=info and info.owner or None,
        comment=comp.comment or None,
        confidence_rating=comp.confidence_rating,
        assay=tuple(Link(_assay_text(a), 'emissionspec', str(a.original_id))
                    for a in assays),
        assay_detail=_text(_assay_detail(a) for a in assays),
        other_sources=other,
        cosmogenic=_cosmogenic(specs),
        radon=_radon(specs),
        **bulk)
    return cells


def _walk(root: Component):
    """ (path, outline, placement, total weight) for every placement below
    `root`, parents first, in order
    """
    def walk(comp, path, outline, total):
        for i, placement in enumerate(getattr(comp, 'children', None) or [], 1):
            here = path + (str(placement.id),)
            number = f"{outline}.{i}" if outline else str(i)
            weight = total * placement.weight
            yield here, number, placement, weight
            yield from walk(placement.component, here, number, weight)
    yield from walk(root, (), '', 1.)


def _sourceterm_counts(obj: Component) -> Dict[tuple, Tuple[int, int]]:
    """ For each path, how many SourceTerms it has of its own, and how many
    of those have no HitEfficiency, as the SourceTerms hold them
    """
    query = SourceTerm.select_version(obj.active_version)(assemblyRoot=obj)
    return {tuple(str(i) for i in doc['_id'] or []):
            (doc['nterms'], doc['nnone'])
            for doc in SourceTerm._get_collection().aggregate([
                {'$match': query._query},
                {'$group': {'_id': '$placement_ids', 'nterms': {'$sum': 1},
                            'nnone': {'$sum': {'$cond': [
                                {'$gt': [{'$size': {'$ifNull': ['$hiteffs',
                                                                []]}}, 0]},
                                0, 1]}}}}])}


def _outputs(version: str) -> Dict[str, Output]:
    config = get_settings(version).hiteffdbconfig
    return {key: Output(config.scalar_unit(key))
            for key, _ in table_columns(version)}


def bom_columns(version: str) -> Tuple[BomColumn, ...]:
    """ The columns of every bill of materials in `version` """
    outs = _outputs(version)
    return _BEFORE + tuple(
        BomColumn(key, name, 'contribution', True, str(outs[key].unit))
        for key, name in table_columns(version))


def _sourceterms_cell(counts, comp: Component, root_id: str) -> tuple:
    """ "# (# w/o hiteff)", linked to the component's SourceTerms page as
    they are within the root
    """
    if not counts:
        return ()
    nterms, nnone = counts
    return (Link(f"{nterms} ({nnone} w/o hiteff)", 'component',
                 str(comp.original_id), view='sourceterms',
                 relativeto=root_id),)


def _build(obj: Component) -> Bom:
    version = obj.active_version
    outs = _outputs(version)
    columns = bom_columns(version)

    tt = term_table(obj)
    sums, _ = path_sums(tt, BudgetFilter(),
                        lambda path: range(len(path) + 1))
    contributions = path_values(tt, outs, sums)
    counts = _sourceterm_counts(obj)
    root_id = str(obj.original_id)
    by_component: Dict[Any, dict] = {}

    def row(path, outline, comp, name, weight, total, location, placement_id):
        key = comp.original_id
        if key not in by_component:
            by_component[key] = _component_cells(comp)
        cells = dict(by_component[key])
        cells.update(
            name=(Link(name, 'component', str(comp.original_id)),),
            placement_id=placement_id,
            weight=weight, total_weight=total,
            location=location or None,
            sourceterms=_sourceterms_cell(counts.get(path), comp, root_id),
            **contributions(path))
        return BomRow(RowKey(path, str(comp.original_id)), outline,
                      len(path), bool(getattr(comp, 'children', None)), cells)

    rows = [row((), '', obj, obj.name, None, 1., obj.location, None)]
    for path, outline, placement, total in _walk(obj):
        comp = placement.component
        rows.append(row(path, outline, comp, placement.name, placement.weight,
                        total, placement.location or comp.location,
                        path[-1]))
    return Bom(columns, tuple(rows))


def bom(obj: Component) -> Bom:
    """ The bill of materials of `obj`, the assembly root of its SourceTerms:
    its own row and then one for each placement below it, at any depth, each
    followed by its own. A row's contributions are for its whole subtree.
    Kept in memory until any data in the version changes, and shared, so must
    not be modified. Raises UnitMismatchError if saved data has units that
    don't fit together.
    """
    return cached_filtered(obj, BudgetFilter(), None, ('bom',),
                           lambda: _build(obj))
