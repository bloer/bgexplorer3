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
- **Identity:** every `VersionedDocument` (Component, Assay and other
  EmissionSpecs, HitEfficiency) is identified by its `original_id`, never by
  `_id`; this holds in URLs (`<objid>`), row keys, links and the cache. A
  `Placement` is an embedded document, so its `id` is a plain ObjectId. Ids are
  handled as strings in rows, as in `placement_names` and
  `SourceTerm.group_keys`.
- **Row key:** a row is uniquely identified by the full list of placement ids
  from the assemblyRoot plus the `original_id` of the row's component.
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
| Hit effs | the HitEfficiencies on this row's own SourceTerms (see below) |
| Contributions | one column per `table_columns(version)` entry (scalars, then ROIs), in its unit |

Hidden by default (toggled with the column-visibility button): component
`original_id`, placement id, surface area, owner, comment, other (non-automatic) isotopes,
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
- `RowKey(placement_ids, component)`: `placement_ids` is the tuple of
  placement-id strings from the root (empty for the root row, the same paths
  as `placement_names`), and `component` is the row's component `original_id`
  as a string. The key is unique within a BoM and is what a diff matches on.
- `BomRow`: a frozen record with a `RowKey`, plus `outline`, `depth`,
  `has_children` and a mapping from column key to a typed cell. A cell is a
  plain value (text or number) or a list of `(text, href)` items. Cells hold
  data only, never HTML.
- `bom(obj) -> Bom`, cached through `cached_filtered`: `obj` is the
  assemblyRoot (SourceTerms with `assemblyRoot=obj`), so there is no
  `relativeto` argument. A `Bom` holds the columns and the ordered rows;
  shared and never mutated.
  Raises `ValueError` for an unknown root, `UnitMismatchError` for bad units
  (same as `table`).
- Walks the hierarchy once for outline, weights and component fields, and
  once over the SourceTerms for contributions and hit efficiencies.

### Hit efficiencies

No matching logic. A HitEfficiency is associated with a row through the
SourceTerms that already carry it: `SourceTerm.hiteffs` holds the matches the
app computed (the same data the relativeto picker counts in
`relativeto_choices`). One aggregation over the SourceTerm collection for
`assemblyRoot=obj` in the active version, grouped by `placement_ids` (the row's
own path, no descendants), collects the distinct `hiteffs` `original_id`s and
the numbers of terms with and without any. Names are then loaded from the
HitEfficiency collection in one `original_id__in` query. The cell lists the
hit efficiencies by name, linked, and notes terms that have none (as the
picker's "no hit efficiencies" badge does). It is the row's own terms, not the
subtree, since hit efficiencies are a property of where a source sits.

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

- Routes `/<objid>/billofmaterials` (page shell; `<objid>` is the component's
  `original_id`) and
  `/<objid>/billofmaterials/rows` (row HTML) on the component blueprint, mirroring
  `view_sourceterms` and its `/rows` endpoint, in
  `application/blueprints.py` `_create_endpoints` for the component class
  only. The BoM does not use `relativeto`; links in the table are ordinary
  `original_id` links to components, assays and hit efficiencies.
- `templates/view_billofmaterials.html` uses `datatable.html` macros; a small
  `bom_rows.html` partial renders `BomRow` cells (the only place cells become
  HTML). A link to the BoM is added to the component page.
- Errors (unknown root, unit mismatch) render through the existing error
  template, as in the results view.

### Future diff (not built here, but not precluded)

- Rows are matched across versions by exact `RowKey` (placement-id path plus
  component `original_id`). A matched row with a differing cell has that
  cell highlighted, old and new value in `title`.
- Hierarchy changes fall out of the key. A row whose parent changed has a
  different path, so it is unmatched in both BoMs. A second pass pairs
  unmatched rows on (last placement id, component `original_id`) and reports
  them as *moved* (outline shown as old → new), then compares their cells. A
  component swapped at the same placement has a different key too and shows as
  removed plus added. Whatever is still unpaired is *added* or *removed*.
  Keyed matching like this is the standard practical approach;
  tree-edit-distance (Zhang–Shasha) is only needed when no stable identity
  exists.
- The diff spec must verify that placement ids survive version copies and
  merges, which the second pass depends on.
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
- Hit efficiencies: a row lists exactly the hit efficiencies on SourceTerms
  with its `placement_ids`; terms without any are flagged; no matching code.
- Row keys: unique within a BoM, built from placement ids and component
  `original_id` (never `_id`).
- Route: page and `/rows` return 200 for a populated version, 404/error for an
  unknown `original_id`.
- Manual: run the app, open the BoM for a populated version, check hidden
  columns, fixed header and left columns, column-visibility and export
  buttons.

## Open details for the plan

- Which isotope names count as "automatic" bulk (U238, Th232, K40 shown;
  everything else in "other isotopes"), read from the existing emission
  source names.
- Where the cosmogenic and radon summaries get their text (reuse existing
  display helpers if present).
- Cost on large versions (the CDMS example): measure `bom()` build time before
  deciding whether lazy `/rows` loading is enough.
- Leave the untracked `*.orig` files, `v1/` and `examples/cdms/` alone.
