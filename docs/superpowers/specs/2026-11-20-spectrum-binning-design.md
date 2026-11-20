# Spectrum binning validation on save: design

## Purpose

Every spectrum with the same name in a version must have the same bin edges, but
nothing enforces it. `Histogram._testbins` only compares the *shape* of the edge
arrays when histograms are combined (`__strictchecking` is off), so spectra with
the same number of bins but different edges (0-100 keV vs 0-200 keV, keV vs MeV,
an offset) are summed bin by bin without any error, giving wrong totals in
source terms, budgets and ROI results. This design moves the check to where the
data enters: a spectrum is rejected on save if its edges are malformed or
disagree with the other spectra of the same name.

This is not mainly a speed-up. The combine-time check is already O(1) and stays
as a backstop. The gains are correctness, errors that name the spectrum and the
hit efficiency instead of a wrong number later, and a guarantee that later work
(ROI weights from the configured binning, ROI ranges checked at settings time)
can rely on.

## Decisions (agreed with the user)

- **Well-formedness** of a histogram's own bins is checked in the field
  (`HistogramField`), nothing else.
- **Consistency** between spectra of the same name follows how
  `HitEffConfig.display_unit` works: the first spectrum with a name defines the
  binning, stored in `HitEffDbConfig.display_spectra[name]`; later ones are
  compared for equality. `HitEfficiency.clean` already loads the version's
  settings, so the check needs no extra query.
- **Replacing a dataset with a new binning** is an explicit admin action, not
  automatic clearing when the last spectrum is deleted: reset the binning, which
  is refused while any spectrum of that name exists. The workflow is: delete the
  old spectra, reset the binning, add the new hit efficiencies.

## Current behaviour

- `HistogramField.validate` checks that it holds a `Histogram`, the value's
  units, `len(hist) == len(bin_edges) - 1` and, if `binsunit` is set, the bin
  units. It doesn't check that the edges are increasing or finite.
- `HitEfficiency.clean` -> `check_dbconfig(dbconfig)` compares each loaded
  scalar and spectrum with `display_unit`, then evaluates the ROIs.
- After a save, `signals.update_hiteff` calls `HitEffDbConfig.update_from`,
  which gives each key without a `display_unit` the unit of the saved hit
  efficiency, and writes the config with `update` (no clean, no signals).
- Import and merge write documents without validation. They afterwards call
  `sourceterm.check_version_units`, which runs `update_from` and `check_dbconfig`
  over every hit efficiency in the version and returns a list of problems.
- `check_settings` (a `VersionSettings.clean_hook`) checks the display units
  against the stored hit efficiencies with an aggregation per key. Bins are
  stored as compressed arrays, so the database can't compare them: nothing
  similar is possible for binning.

## Design

### 1. Well-formed edges: `HistogramField.validate`

mongoengine fields have `validate`, not `clean` (`clean` is the document-level
hook), so the checks go in `HistogramField.validate`, which already has the
length check. Add, in a shared function `check_bin_edges(edges)` in
`histogram.py` so that the consistency check can call it first:

- the histogram values and the edges are one-dimensional
- at least one bin (two edges)
- edges are finite, and strictly increasing
- edges have a unit (or none) that `binsunit` accepts, as now

Messages say what is wrong ("bin edges are not increasing at index 17"). Errors
raised here are field-level, so they carry the path of the spectrum.
`check_dbconfig` calls `check_bin_edges` before comparing, because
`HitEfficiency.clean` runs before the field validation and a malformed array
would otherwise be reported as a mismatch.

### 2. Configured binning: `HitEffConfig.bin_edges`

A new optional field on `HitEffConfig`, `None` by default, used by spectra
(`display_spectra`) only, as `link_spectrum` is used by scalars only. It stores
the edges as the Quantity array a histogram stores them (`bins`, `binsunit`
through `compress`), not a digest, so that edges can be compared with a
tolerance and described in error messages. Typical spectra have hundreds to a
few thousand bins, which is small compared with the settings document's use.

A subclass for spectra was considered and rejected: `display_spectra` documents
are already stored with `_cls: 'HitEffConfig'`, so they wouldn't load as the
subclass without a migration.

Same binning (`histogram.bins_match(a, b, reference)`): same length, same
dimensionality, and after converting `a` to the unit of `b`, every edge within an
absolute tolerance of `1e-6` of the smallest bin width of the configured
binning. Exact equality would reject keV vs MeV conversions that differ in the
last bit.

### 3. Set and check

- **Check** in `HitEfficiency.check_dbconfig`, in the same loop over loaded
  spectra that checks units, for a spectrum whose config has `bin_edges`:
  `check_bin_edges`, then `bins_match`. Raises `ValidationError` naming the
  spectrum, the bins found (count, range, unit) and the configured ones, and
  that an administrator can reset the binning if the spectra are to be replaced.
  A `None` spectrum and a config without `bin_edges` are skipped. Spectra that
  weren't loaded are left alone, as now.
- **Set** in `HitEffDbConfig.update_from`, which already runs after each hit
  efficiency save and in the audit: a spectrum config with `bin_edges is None`
  takes the saved spectrum's edges. It is written by the same `update` as the
  units.

So a hit efficiency's first spectrum of a name defines the binning, and a
second one with other edges is rejected, exactly as for units. Spectra are
compared on save for the web, API and CLI alike, as all of them go through
`HitEfficiency.save`.

### 4. Replacing a dataset: reset the binning

An administrator action `reset_binning(settings, key)` in `hiteff.py`:

1. for every tag in `settings.version_tags`, count the version's hit
   efficiencies with `spectra_keys=key` (an indexed field); if any, raise an
   error naming the tags and counts, and the delete-first workflow
2. otherwise set `display_spectra[key].bin_edges = None`, written with `update`
   like `update_from` (not a settings `save`, which would re-run the checks and
   the ROIs), then `settings.touch(version)`

The UI is a button on the spectrum display table of the settings page, for
admins (`require(Role.admin)`, as `unlock` does), next to a read-only summary of
the configured binning ("1500 bins, 0-3000 keV"). It runs as a job like other
settings edits, so it holds the version's lock and a concurrent save can't add a
spectrum between the count and the update. There is no UI to delete a
`display_spectra` entry; reset is the supported route, and an entry that is
deleted through the API is rebuilt by `update_from` from whichever spectrum is
saved next (see Limits).

Resetting only affects the active version's settings. A branch has its own
settings document from the first edit, so another version that still holds the
old spectra keeps the old binning, and merging the two reports the conflict (5).

### 5. Settings, import and merge

- `bin_edges` is not a column of the settings form and must survive saving it:
  `update_object` edits the existing `HitEffConfig` in place. The settings API
  (`HITEFFDBCONFIG_FIELDS`, `display_spectra`) must not accept `bin_edges`, so
  that it can only be changed by a hit efficiency or a reset. To verify while
  implementing: that replacing `display_spectra` wholesale through the API keeps
  the existing `bin_edges` of unchanged keys.
- `check_settings` doesn't re-check stored hit efficiencies against
  `bin_edges`: it can't query for it (see Current behaviour), and the settings
  don't change it.
- `check_version_units` already runs `update_from` and `check_dbconfig` over all
  hit efficiencies after an import or a merge, so with the above it defines the
  binning of spectra that don't have one and returns a problem for each
  mismatched spectrum, with no further code. Its docstring and the message
  mentioned in `app.py` say "units"; they should say "units and binning".
- Existing versions have no `bin_edges`. They get them the first time a hit
  efficiency is saved or the audit runs. Run the audit once after upgrading to
  find versions that already hold inconsistent spectra; a CLI command for that is
  an open question.

### 6. Combining

`Histogram._testbins` and `sum_all` are unchanged: the shape check stays as a
backstop for data that bypassed validation. Strict checking remains off.

## Limits

- The first spectrum of a name defines the binning. If the entry is missing
  while spectra exist (deleted through the API, or data from before this
  change), whichever is saved first wins, and the others show up as mismatches in
  the audit, not on save.
- Two saves that both define the binning at the same moment can both pass the
  check before either `update_from` runs. Job-based edits are serialised by the
  version lock; direct API or CLI saves are not. The audit finds the result.
- The configured edges keep the unit of the first spectrum. Later spectra are
  compared after conversion, and are stored as they were given, not rewritten.

## Testing

- `check_bin_edges`/`HistogramField.validate`: decreasing, repeated, NaN, inf,
  2-D, empty and length-mismatched edges all give field-level errors with the
  spectrum's path; a good histogram, including unit-less default edges, passes.
- `bins_match`: equal, MeV vs keV, within and beyond the tolerance, different
  length, different dimensionality.
- Save: the first hit efficiency sets `bin_edges`; a second with the same
  edges in another unit saves; one with other edges is rejected with the message
  naming the spectrum; another hit efficiency with a differently named spectrum
  is unaffected; a `None` spectrum is skipped.
- Reset: refused while any spectrum of that name exists in any tag of the
  settings; after deleting them it clears the binning, and new edges are
  accepted; it doesn't run the ROIs or change other settings; non-admins get 403;
  the job holds the lock.
- Settings form and API round trips keep `bin_edges`; the API rejects it as
  input.
- Import and merge: the audit sets missing binning and reports a conflict
  between versions with different binning.
- Existing tests that build hit efficiencies with differently binned spectra
  of the same name (to be found when running the suite) are fixed to be
  consistent, or are testing the backstop and keep going through `Histogram`
  directly.

## Docs

- `docs/settings.md`: the spectrum display table's binning, that the first
  spectrum defines it, and how to replace it (delete, reset, re-add).
- `docs/admin.md`: the reset action.
- `docs/model.md` or the hit efficiency section: what a valid histogram is.

## Open questions

- Tolerance: `1e-6` of the smallest bin width is a guess; is there data with
  edges that differ by more through rounding in the writer?
- Reset through the API and CLI as well as the UI, for scripted workflows?
- A CLI command to run the audit (and so populate binning) over all versions
  after upgrading, instead of waiting for the first save?
- Should a failed save report all mismatched spectra of a hit efficiency at once,
  rather than the first?

## Not in this design

- ROI weights computed once per ROI from the configured binning, which would
  also let settings validation reject an ROI outside the spectrum's range
  without any hit efficiency. This becomes possible once the binning is
  guaranteed, and is a follow-up.
