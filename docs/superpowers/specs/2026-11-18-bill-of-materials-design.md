# Bill of Materials view: design

## Purpose

One overview table of an assembly's inputs in a version: one row per assembly
path (chain of placement ids below the assemblyRoot), carrying identity,
quantity, material, assay and exposure details, and the row's contribution to
every displayed scalar and ROI. It replaces the v1 `billofmaterials` page
(`v1/templates/billofmaterials.html`, 18 plain columns) with the information
the v2 model now holds.

Follow-up (separate spec and plan): a diff view comparing the BoMs of two
versions. This design builds the BoM as structured rows so that diff is a
cell-by-cell comparison, but does not build the diff.

## Decisions (agreed with the user)

- **Scope:** per assemblyRoot, at `/explore/<active_version>/component/<objid>/billofmaterials`
  on the existing component blueprint. The component is the assemblyRoot; "total
  weight" is relative to it. No version-level page.
- **Contributions:** each scalar/ROI column is the *inclusive subtree total*
  for that path (the path and everything beneath it); a leaf equals its own
  contribution.
- **Columns:** the compact set below, IDs and metadata hidden by default,
  Outline and Name fixed on the left.
- **Tooling:** the bundled DataTables (1.13.8 with Buttons 2.4.2,
  FixedColumns, FixedHeader, copy/CSV/Excel) lacks the Buttons
  column-visibility module (`colvis`). Add that module at the same Buttons
  version, either by rebuilding `static/datatables.min.{js,css}` or by adding
  the single `buttons.colVis` file; choose whichever is obtainable offline.
- **Caching:** `ResultsCache` (see `models/budget/grouping.py`
  `cached_filtered`), keyed on the version's change token; self-invalidating,
  tags effectively permanent. No persisted store and no background job unless
  measured build time shows it is needed.

## Columns

Fixed (always shown): **Outline** (1.2.3.4 style, 1-based index per level,
following `placement_names` order) and **Name** (placement label or component
name, linked to the component).

Shown by default:

| Column | Content |
|---|---|
| Description | `Component.description` |
| Wt (parent) | `Placement.weight` in the immediate parent |
| Total wt | product of placement weights from the root to this row |
| Material | `Component.material` |
| Mass | `Component.mass` (not multiplied by weights) |
| Part | `"Vendor / partnum"` from `purchaseinfo`, linked if a link is set |
| Assay | `"name (publication)"` per attached Assay, one line each, linked to the assay |
| U238, Th232, K40 | bulk values from the component's emission specs |
| Cosmogenic | summary of attached `CosmogenicActivation` periods |
| Radon | summary of attached `RadonExposure` periods |
| Hit effs | count and names of matching HitEfficiencies |
| Contributions | one column per `table_columns(version)` entry (scalars, then ROIs), in its unit |

Hidden by default (toggled with the column-visibility button): component id,
placement id, surface area, owner, comment, other (non-automatic) isotopes,
assay detail (sample, measurement, radiopurity id), confidence rating,
location.

Sub-assembly rows are shaded; long text cells are truncated with the full text
in the `title` attribute. Ordering and paging are off, since the hierarchy is
the order. Header fixed (FixedHeader or `scrollY`), Outline and Name pinned
(FixedColumns, two left columns). Buttons: column visibility (with restore),
copy, CSV, Excel.

## Architecture

### Data layer: `models/budget/bom.py`

- `BomColumn(key, label, group, visible)`: the single declaration of the
  columns, driving table headers, the default visibility, and (later) the diff.
  Contribution columns are generated from `table_columns(version)`.
- `BomRow`: a frozen record keyed by `path` (tuple of placement-id strings,
  the same keys as `placement_names`), plus `outline`, `depth`,
  `has_children` and a mapping from column key to a typed cell. A cell is a
  plain value (text or number) or a list of `(text, href)` items. Cells hold
  data only, never HTML.
- `bom(obj, relativeto=None) -> Bom` (decorated with `filtered_cached`): a
  `Bom` holds the columns and the ordered rows; shared and never mutated.
  Raises `ValueError` for an unknown root, `UnitMismatchError` for bad units
  (same as `table`).
- Walks the hierarchy once for outline, weights and component fields, and
  once for contributions.

### Inclusive contributions

`table()` in `models/budget/table.py` does **not** give inclusive subtree
totals. `levels()` in `grouping.py` sums each SourceTerm only into its
first-level placement, its own depth-level placement, and (with `total`) the
root, so intermediate ancestors are missed at any depth. The BoM needs every
prefix of each term's component path.

Plan: factor the term-to-path summing in `table()` into a shared helper that
takes the set of prefix lengths to sum into, and call it with all prefixes for
the BoM. `table()` behaviour is unchanged; its tests must still pass. The
unit handling (`Output`, `get_settings(...).hiteffdbconfig`) is reused as-is.

### View

- Routes `/<objid>/billofmaterials` (page shell) and
  `/<objid>/billofmaterials/rows` (row HTML) on the component blueprint, mirroring
  `view_sourceterms` and its `/rows` endpoint, in
  `application/blueprints.py` `_create_endpoints` for the component class
  only. `relativeto` is honoured via the existing `application/relativeto.py`
  mechanism and carried on all outgoing links.
- `templates/view_billofmaterials.html` uses `datatable.html` macros; a small
  `bom_rows.html` partial renders `BomRow` cells (the only place cells become
  HTML). A link to the BoM is added to the component page.
- Errors (unknown root, unit mismatch) render through the existing error
  template, as in the results view.

### Future diff (not built here, but not precluded)

- Rows match across versions by `path` (placement ids). Placement ids are
  `ObjectIdField`s on embedded documents; whether they survive version
  copies and merges must be verified in the diff spec. If they do not, match
  on component `original_id` plus the parent's match.
- Same key, different cell: highlight the cell, old and new value in `title`.
- Same placement id under a different parent: a *moved* row (outline shown as
  old → new). Present in only one BoM: *added* or *removed*. Keyed matching is
  the standard practical approach; tree-edit-distance (Zhang–Shasha) is
  only needed when no stable identity exists.
- Because rows are typed cells keyed by column key, the diff is a dict
  comparison and needs no HTML parsing.

## Testing

`unittest`, real mongod via `tests/dbutil.py` (`BGEXPLORER_TEST_MONGODB_URI`),
modelled on `test_budget.py` and `test_app_components.py`.

- Builder: outline numbering; total-weight product; one component placed
  under two parents gives two rows with distinct paths; component with
  several assays; assembly without material/mass; missing optional fields.
- Contributions: for a small tree, an assembly row equals the sum of its
  descendants for each scalar and ROI; the root equals `table()`'s root total;
  `table()` results are unchanged by the refactor.
- Cache: second call returns the same object; an edit to any component
  invalidates it.
- Route: page and `/rows` return 200 for a populated version, 404/error for an
  unknown id, links keep `relativeto`.
- Manual: run the app, open the BoM for a populated version, check hidden
  columns, fixed header and left columns, column-visibility and export
  buttons.

## Open details for the plan

- Exact rule for "associated HitEfficiencies": hit efficiencies match on
  `(source, location, material)`; use the same matching code SourceTerm
  creation uses, including `ComponentSpec`/`Placement` location overrides,
  rather than re-deriving it.
- Which isotope names count as "automatic" bulk (U238, Th232, K40 shown;
  everything else in "other isotopes"), read from the existing emission
  source names.
- Where the cosmogenic and radon summaries get their text (reuse existing
  display helpers if present).
- Cost on large versions (the CDMS example): measure `bom()` build time before
  deciding whether lazy `/rows` loading is enough.
- Leave the untracked `*.orig` files, `v1/` and `examples/cdms/` alone.
