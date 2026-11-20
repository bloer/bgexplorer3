# Spectrum binning validation Implementation Plan

> **For agentic workers:** implement task by task, test first; commit per task.

**Goal:** reject spectra with malformed bins, or bins that differ from the other spectra of the same name, when a hit efficiency is saved.

**Architecture:** `check_bin_edges` and `bins_match` in `histogram.py`; `HistogramField.validate` checks well-formedness; the first spectrum of a name stores its edges in `HitEffConfig.bin_edges` (set by `HitEffDbConfig.update_from`, checked by `HitEfficiency.check_dbconfig`); an admin reset (`reset_binning`, refused while spectra of the name exist) replaces the binning.

**Tech Stack:** mongoengine, pint, numpy, Flask, unittest.

**Spec:** `docs/superpowers/specs/2026-11-20-spectrum-binning-design.md`

## Global Constraints

- Tolerance: 1e-6 of the smallest configured bin width, absolute.
- No change to job serialization, locks, or the spectrum/bulk routes.
- No migration or audit command. `bin_edges` is never accepted from API clients and is left out of settings JSON.

## Review Focus

- Settings form save must not drop `bin_edges` (update_object builds new entries).
- A PATCH that round-trips `display_spectra` keeps `bin_edges`.
- Existing tests with same-named, differently binned spectra.
- Unit-less default edges (`Histogram(hist)` with no edges) still pass.
- A malformed array in `check_dbconfig` reports malformed, not mismatch.

---

### Task 1: `check_bin_edges`, `bins_match`, field validation
Files: `models/histogram.py`, `models/fields.py` (HistogramField.validate, new `BinEdgesField`), `tests/test_histogram.py`, `tests/test_fields.py`.
- `check_bin_edges(edges)` raises ValueError (1-D, >=2, finite, strictly increasing); `HistogramField.validate` calls it via `self.error`.
- `bins_match(a, b)` -> bool, `b` is the reference.
- `BinEdgesField`: stores `compress({'bins', 'binsunit'})`, returns a Quantity.

### Task 2: `HitEffConfig.bin_edges`, set and check
Files: `models/settings.py`, `models/hiteff.py`, `tests/test_hiteff.py` (+ fixing existing tests).
- `update_from` sets `bin_edges` on spectra configs; `check_dbconfig` collects mismatches into one ValidationError.
- `signals.update_hiteff` already writes the config with `update`; check it persists `bin_edges`.

### Task 3: reset, form and API
Files: `models/hiteff.py` (`require_no_spectra`, `reset_binning`), `application/forms.py` (keep existing map entries), `application/api.py`, `application/versions.py`, `application/job_kinds.py`, `templates/edit_settings.html`, tests.

### Task 4: import/merge wording, docs
Files: `models/sourceterm.py` docstring, `app.py` message, `docs/settings.md`, `docs/admin.md`, `docs/model.md`.
