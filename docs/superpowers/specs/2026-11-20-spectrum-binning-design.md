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
- **Through the API**, the settings PATCH already replaces `display_spectra`
  wholesale, so an entry can be deleted there. No new endpoint: deleting an
  entry that has a configured binning is the reset, and is refused under the
  same condition (spectra of that name exist). Clients can't set `bin_edges`.
- Alpha: no migration and no audit command. Existing versions get their binning
  the next time a hit efficiency is saved or a version is imported or merged.
- A failed save lists the binning mismatch of every spectrum of the hit
  efficiency, not just the first.

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
  `check_bin_edges`, then `bins_match`. The problems are collected over the
  loop and raised as one `ValidationError`, a line per spectrum giving its name,
  the bins found (count, range, unit) and the configured ones, ending with that
  an administrator can reset the binning if the spectra are to be replaced.
  Collecting costs a list and one `raise` after the loop; unit errors are
  raised as they are now. A `None` spectrum and a config without `bin_edges`
  are skipped. Spectra that weren't loaded are left alone, as now.
- **Set** in `HitEffDbConfig.update_from`, which already runs after each hit
  efficiency save and in `check_version_units`: a spectrum config with `bin_edges is None`
  takes the saved spectrum's edges. It is written by the same `update` as the
  units.

So a hit efficiency's first spectrum of a name defines the binning, and a
second one with other edges is rejected, exactly as for units. Spectra are
compared on save for the web, API and CLI alike, as all of them go through
`HitEfficiency.save`.

### 4. Replacing a dataset: reset the binning

Two functions in `hiteff.py`:

- `require_no_spectra(settings, key)`: for every tag in `settings.version_tags`,
  count the version's hit efficiencies with `spectra_keys=key` (an indexed
  field); if any, raise `ValidationError` naming the tags and counts and the
  delete-first workflow.
- `reset_binning(settings, key)`: `require_no_spectra`, then set
  `display_spectra[key].bin_edges = None`, written with `update` like
  `update_from` (not a settings `save`, which would re-run the checks and the
  ROIs), then `settings.touch(version)`.

**UI**: a button on the spectrum display table of the settings page, for admins
(`require(Role.admin)`, as `unlock` does), next to a read-only summary of the
configured binning ("1500 bins, 0-3000 keV"). It is a new job kind in the
`EDIT` class (`job_kinds.py`), submitted with the version as its scope. Those
are serial per scope, so it can't run at the same time as a hit efficiency
form save or a settings save for that version. The form has no way to delete a
`display_spectra` entry; reset is the route there.

**API**: the settings PATCH sets `display_spectra` as a whole (`update_settings`
parses the JSON into new `HitEffConfig`s), which has two consequences handled
in `update_settings`, with no new route:

- `bin_edges` is not accepted from clients. Each entry that remains takes the
  `bin_edges` of the existing entry with the same key, so a round trip of the
  config, or a PATCH that omits it, can't change it. Settings JSON leaves
  `bin_edges` out (it's compressed binary); the binning summary above is
  worth adding there only if clients ask.
- An existing entry with `bin_edges` that is not in the new map is a reset: it
  calls `require_no_spectra` and fails with a 409-style `APIError` naming the
  key if spectra of that name exist. With none, the entry is deleted, and the
  next hit efficiency of that name creates it again. Deleting an entry without
  a binning needs no check.

Edits are serialised by the job scope, not by the version lock; see Limits.

Resetting only affects the active version's settings. A branch has its own
settings document from the first edit, so another version that still holds the
old spectra keeps the old binning, and merging the two reports the conflict (5).

### 5. Settings, import and merge

- `bin_edges` is not a column of the settings form and must survive saving it:
  `update_object` edits the existing `HitEffConfig` in place (to verify while
  implementing). The API is handled in 4, so `bin_edges` only changes through a
  hit efficiency or a reset.
- `check_settings` doesn't re-check stored hit efficiencies against
  `bin_edges`: it can't query for it (see Current behaviour), and the settings
  don't change it.
- `check_version_units` already runs `update_from` and `check_dbconfig` over all
  hit efficiencies after an import or a merge, so with the above it defines the
  binning of spectra that don't have one and returns a problem for each
  mismatched spectrum, with no further code. Its docstring and the message
  mentioned in `app.py` say "units"; they should say "units and binning".
- Existing versions have no `bin_edges`. They get them the first time a hit
  efficiency is saved, or an import or merge runs `check_version_units`. There
  is no migration or audit of data that is already inconsistent (alpha).

### 6. Combining

`Histogram._testbins` and `sum_all` are unchanged: the shape check stays as a
backstop for data that bypassed validation. Strict checking remains off.

## Limits

- The first spectrum of a name defines the binning. If the binning is missing
  while spectra exist (data from before this change), whichever is saved first
  wins, and an older spectrum that disagrees is only noticed when it is saved
  again or its version is imported or merged.
- Concurrency. The web UI and the API submit their edits (documents, settings,
  version updates) as the same serial jobs, one at a time per version, so those
  can't race. They aren't serialised by the version lock: that is taken only by
  the long whole-version operations (create, delete, import, merge), and the
  write guard makes everyone else's writes to a locked version fail or wait.
  Not serialised are the writers that skip the job queue: the spectrum import,
  rename and delete routes of a hit efficiency, the bulk delete and clone
  routes, and scripts or the CLI that save models directly. Two of them
  defining the same binning at the same moment can both pass the check before
  either `update_from` runs, and one adding a spectrum can land between the
  count and the update of a reset. The window is a few milliseconds, but the
  spectrum routes are the entry point for this feature, so moving them into
  `EDIT` jobs is a worthwhile follow-up (not part of this design).
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
  it is submitted as an `EDIT` job scoped to the version.
- Settings form and API round trips keep `bin_edges`; a PATCH can't set or
  clear it; a PATCH that drops an entry with a binning is refused while spectra
  of that name exist and deletes it otherwise; dropping an entry without
  binning is allowed.
- A failed save with several mismatched spectra lists them all.
- Import and merge: `check_version_units` sets missing binning and reports a
  conflict between versions with different binning.
- Existing tests that build hit efficiencies with differently binned spectra
  of the same name (to be found when running the suite) are fixed to be
  consistent, or are testing the backstop and keep going through `Histogram`
  directly.

## Docs

- `docs/settings.md`: the spectrum display table's binning, that the first
  spectrum defines it, and how to replace it (delete, reset, re-add).
- `docs/admin.md`: the reset action.
- `docs/model.md` or the hit efficiency section: what a valid histogram is.

## Not in this design

- Moving the hit efficiency spectrum routes and the bulk routes into serial
  jobs (see Limits).
- ROI weights computed once per ROI from the configured binning, which would
  also let settings validation reject an ROI outside the spectrum's range
  without any hit efficiency. This becomes possible once the binning is
  guaranteed, and is a follow-up.
