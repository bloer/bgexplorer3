# Prune redundant unit checks

## Goal

Check units once, when data is saved, in `EmissionSource.clean`,
`Component` (its `QuantityField` units) and `HitEfficiency.clean`, and match
SourceTerms only to HitEfficiencies whose norm fits the source's multiplier.
Remove the SourceTerm-level checks, which only repeat what those checks
already guarantee.

## Why the save-time checks are enough

For every `Multiplier`, `units(valid_units) × getvalue(component)` has the
units of the matching `NormMultiplier`:

| Multiplier | rate unit | × component | = norm |
|---|---|---|---|
| mass | Bq/kg | kg | rate (1/s) |
| volume | Bq/m³ | m³ | rate |
| surface, inner_surface, outer_surface | Bq/m² | m² | rate |
| length | Bq/m | m | rate |
| per_piece | Bq | 1 | rate |
| flux | 1/cm²/s | 1 | flux |
| flux_per_sr | 1/cm²/s/sr | 1 | flux_per_sr |

Each link in the chain is checked on its own:

- `EmissionSource.clean` makes sure the rate fits its multiplier. For an
  isotope, a mass rate (ppb, g/m², g, …) is allowed and converted to Bq by
  `standardized_rate`.
- `Component` field units fix the dimensions of mass, volume, area and length.
- `hiteffs_query` and the reverse match in `signals.update_hiteff` only pair a
  term with HitEfficiencies whose `norm == source.multiplier.norm`.
- `HitEfficiency.check_dbconfig` and `VersionSettings._check_display_unit` /
  `_check_roi_unit` make sure `value × norm.units` fits the display unit.

So `rate × multiplier × value` always converts to the display unit, and
nothing needs to be checked per SourceTerm.

## Changes

### Fix the last commit

- `Multiplier.check_units`: `units(self.valid_units).check(...)`, because
  `valid_units` is a string.
- `hiteffs_query`: `self.source.multiplier` rather than the nonexistent
  `self.source_multiplier`.
- `standardized_rate`: call `concentration_to_rate(name, rate)` with the
  arguments in the right order.
- `EmissionSource.clean`:
  - With no rate there's nothing to check, and the multiplier may stay None.
  - A rate that is neither recognised nor a convertible isotope mass raises a
    `ValidationError` on `rate`, not an AttributeError.

### emissionspec.py

- `Multiplier.valid_units`:
  - flux becomes `1/cm**2/s`, which equals the current `Bq/cm**2`.
  - flux_per_sr becomes `1/cm**2/s/sr`.
- New property `Multiplier.norm` returns the `hiteff.NormMultiplier` for each
  multiplier. It is the single mapping, used by both match directions.
  - `hiteff.py` doesn't import `emissionspec`, so there's no import cycle.
- `get_multiplier` never infers flux or flux_per_sr; they can't be told apart
  from surface activity by dimension. Remove the unreachable `1/cm**2/s/sr`
  branch, so `1/cm²/s` becomes `surface`. Flux sources must set `multiplier`
  on the source or the spec.
- `getvalue`: per_piece, flux and flux_per_sr give 1.

### hiteff.py

- Remove `NormMultiplier.none` and `NormMultiplier.check`.
- Remove `check_sourceterms` and its call in `clean`.

### sourceterm.py

- Remove:
  - `check_units` and its calls in `clean` and `upsert`
  - `unit_checks_deferred` and `_unit_checks_deferred`
  - `hiteff_rate_units`
  - the dummy `unweighted_rate` (callers use `source.standardized_rate`)
  - the norm check in `CalculatedResults._calculate`
- `hiteffs_query`:
  - filter on `norm=self.source.multiplier.norm`
  - with no multiplier (no rate), match nothing
- `find_hiteffs(replace=True)` also replaces the list when it is empty, so
  HitEfficiencies that no longer match are removed when a source's
  multiplier changes.
- `check_version_units(version)` becomes a per-document audit, still used
  after an import or merge:
  1. Give missing display units from the HitEfficiencies, as now.
  2. Run `check_dbconfig` on each HitEfficiency (scalars and spectra loaded,
     ROIs not stored).
  3. Run `validate()` on each EmissionSource of each EmissionSpec.
  4. Return and log the problems, deduplicated, without raising.

### signals.py

- `update_hiteff` also filters the terms it matches on
  `source__multiplier__in` the multipliers whose `norm` is the hiteff's norm.
  That way, changing a HitEfficiency's norm detaches it from terms it no
  longer fits.

### termcalc.py

- Remove `_check_norm`, `check`, `check_hiteff`, `_norms` and the `strict`
  flag.
- `_factor` keeps raising `UnitMismatchError` when a conversion fails or a key
  has no display unit. This is the conversion itself, so it costs nothing, and
  it still catches data saved with `validate=False`.
- `_rate` uses `sourceterm.source.standardized_rate`.

### versionfile.py, versioncontrol.py

- Drop the `unit_checks_deferred()` wrappers.
- Keep calling `check_version_units` afterwards.

### Kept as is

- `VersionSettings._check_display_unit` and `_check_roi_unit`, and
  `SpectrumROI.evaluate`. They are the settings side of the HitEfficiency ↔
  display-unit check.
- `UnitMismatchError` and the app's error handler for it.
- `common.validate_unit`. It's unrelated and has its own test.

### Examples

- `examples/qis/qis.py`: Gammaflux spec uses `multiplier='flux'`.
- `examples/cdms`:
  - `none` becomes `flux` for flux distributions and `per_piece` otherwise.
  - `surface_in` / `surface_out` become `inner_surface` / `outer_surface`.
  - `geometry.py:113` gets the same mapping.

## Tests

Rewrite `tests/test_unitcheck.py`, and update `tests/test_emissionspec.py`
and `tests/test_budget.py`, which use the removed names.

**Rejected at clean** (`ValidationError`, nothing saved):

- an explicit multiplier with a rate that doesn't fit it (Bq/kg with
  `surface`; Bq with `mass`)
- unrecognised rate units (`1/s/kg**2`)
- a ppb rate, or g/m², on a source that isn't an isotope
- a ppb rate with `multiplier=flux` on an isotope
- a component mass entered in metres
- HitEfficiency scalars that don't fit an existing display unit

**Accepted at clean:**

- an isotope given as ppb (mass), g/m² (surface) or g (per_piece)
- a rate of None
- a flux source with an explicit `flux` or `flux_per_sr` multiplier

**HitEfficiency matching.** Rate, flux and flux_per_sr HitEfficiencies for
the same source and location:

- A `Bq/kg` source matches only the rate HitEfficiency, and its result equals
  value × rate × mass.
- A `ppb` isotope source matches only the rate HitEfficiency, and its result
  uses `concentration_to_rate`.
- `Bq/m²` with `inner_surface` matches the rate HitEfficiency, using the inner
  surface area.
- `1/cm²/s` with `flux` matches only the flux HitEfficiency; with
  `flux_per_sr`, only the flux_per_sr one.
- A source with no rate matches none.
- A HitEfficiency saved after the term exists is attached only if its norm
  fits.
- Changing a HitEfficiency's norm detaches it.
- Changing a source's multiplier from mass to flux swaps its
  HitEfficiencies, including down to none.

**Audit:**

- Data inserted raw with a HitEfficiency whose units don't fit the display
  unit is reported by `check_version_units`.
- Missing display units are filled in.
- Calculating that data raises `UnitMismatchError`, which the page reports.

Run the full test suite, then load the qis example.
