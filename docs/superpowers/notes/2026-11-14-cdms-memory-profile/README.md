# CDMS model: timings and memory findings

Status: findings only, no code changed. Written to resume the memory work
later, separately from the background-jobs design
(`docs/superpowers/specs/2026-11-13-background-jobs-design.md`).

## What was measured

Model: version `examples-cdms7tower` in MongoDB database
`bexplorer3_testing_cdms` (127.0.0.1:27017). 618 components (118 assemblies,
8 hierarchy levels, 785 placements, one root `Total`, no multi-parent
assemblies), 40,758 SourceTerms, 4,202 hit efficiencies, 62 assays.

Every operation ran on a temporary branch of the version, deleted afterwards,
so the model itself was not changed. The Python environment is
`/home/agent/workspace/.venv`.

Reproduce (from the repo root):

    python docs/superpowers/notes/2026-11-14-cdms-memory-profile/profile_cdms.py --ops view,spectra,export --json out.json
    python docs/superpowers/notes/2026-11-14-cdms-memory-profile/tm_cdms.py view,spectra,root_weights

`profile_cdms.py` adapts the ops of `scripts/profile_standard.py` to this
model (root `Total`, an existing ROI spectrum, the assay used by most
components). `tm_cdms.py` adds tracemalloc and keeps a snapshot at the
high-water mark of each step, printed by line and by call stack. tracemalloc
makes steps about 15 to 20 times slower, so use it for attribution only, not
timing. The logs here are the output of the runs below with the "Error
calculating livetime" lines removed (see Observations).

## Timings (unprofiled, wall clock)

| Operation | Time | Peak RSS above start |
|---|---|---|
| Add an ROI to the settings | 167 s | small |
| Rebuild SourceTerms | 137 s | +541 MB |
| Edit an assay (`brass`, used by 89 components) | 77 s | small |
| Double the root's 5 placement weights | 35 s | +604 MB |
| Add a leaf and place it in `Cavern` | 0.2 s | small |
| Export the version (290 MB file) | 350 s | +213 MB |
| Cold `term_table` of the root | 8.7 s | +461 MB |
| Cold spectra of the root | 11.1 s | +1726 MB |
| Create a branch | 6 to 9 s | |

After the ROI, weights and rebuild edits the next cold `term_table` takes
8 to 9 s again, since the term cache is replaced. After the assay and leaf
edits it takes 0.25 to 0.7 s.

## Memory: what is stored

`HitEfficiency` documents are almost all of the data; SourceTerms are small.

| Document | Total stored | Large fields |
|---|---|---|
| `HitEfficiency` (4,202) | about 2.5 GB | `spectra` 2.35 GB (587 KB per document); `extra_metadata` 134 MB (33 KB per document, nested dicts); the rest is under 3 MB |
| `SourceTerm` (40,758) | about 35 MB | `source` 11 MB, `assemblyPath` 7 MB |

## Memory: where it goes (tracemalloc, traced peak above step start)

RSS peaks were 4 to 8 times the traced peaks (tracemalloc overhead plus the
allocator keeping freed memory); resident memory stayed 2 to 5 GB after each
step.

| Step | Traced peak | Holders at the high-water mark |
|---|---|---|
| Cold `term_table` | 307 to 411 MB | BSON decoding 193 to 226 MB (3.8 to 4.6 M small objects); mongoengine dict-field conversion (`base/fields.py:446`, three nested levels) 45 to 65 MB. Loaded by `load_hiteffs(spectra=False)` (`sourceterm.py:512`), which excludes only `spectra`, so `extra_metadata` is decoded for every hit efficiency |
| Root-weights save | 857 MB | BSON decoding 487 MB (10.3 M objects, over twice the view's), dict-field conversion 159 MB. `HitEffIndex.load` (`sourceterm.py:592`) loads every hit efficiency for the sources and locations involved, excluding only `spectra`; it is built per `sync_sourceterms` call, i.e. per level of the `update_parent` recursion |
| Cold spectra | 1.9 GB | `asymmetric.py:177` 640 MB, `arrays.py:42` 303 MB, `asymmetric.py:378` and `:384` 265 MB each, BSON decoding 169 MB, dict-field conversion 47 MB |

The cold spectra lines, by what they are:

- `arrays.py:42` (303 MB): `decode_array` makes a writable `.copy()` of every
  array decoded from the stored spectra.
- `asymmetric.py:177` (640 MB): `_frozen` makes another copy (`np.array`) of
  `mode`, `s0` and `s1` in every `AsymmetricUncertainty`, to make it
  read-only. Applies to decoded and to calculated results.
- `asymmetric.py:378` and `:384` (530 MB): `v0` and `v1`, the squares of
  `s0` and `s1`, cached on each instance the first time they are used.
  `modesq` (line 390) is the same pattern; it didn't make the top of the list
  but follows the same rule.
- The rest is the stored spectra decoded by BSON (169 MB for the share live at
  that moment).

Caveat: not separated yet is how much of the 1.9 GB is the retained result,
the per-term spectra in the `TermTable`, which `_spectra_cache` holds
(`cache_bytes(.3)`), and how much is transient decoding. The chunked loading
in `_spectrum_records` (`budget/terms.py:109`) already bounds the transient
part per chunk, but the records it yields keep their arrays.

## Paths forward

Cheap and independent of each other; re-measure after each.

### Scalar view and weights save (about half of both peaks)

1. Exclude `extra_metadata` (134 MB stored) from `load_hiteffs` and
   `HitEffIndex.load` when it isn't needed. First check that nothing on those
   paths reads it. Expect roughly half of the 193 to 226 MB BSON decode.
2. Narrow `HitEffIndex.load` as the TODO at `sourceterm.py:597` says, to the
   location, material and norm needed.
3. Share one `HitEffIndex` across the recursive `update_parent` calls of a
   save instead of rebuilding it per level. Unconfirmed that this is the
   cause of the 10 M objects; confirm by counting `load` calls during the
   weights save.

### Cold spectra (1.9 GB traced, 1.7 GB RSS)

Ordered by expected saving over effort. The first two are the easiest.

A. **Drop the two array copies.** `np.frombuffer` over the decompressed bytes
   is already read-only, which is what `_frozen` wants.
   - `decode_array` (`arrays.py:42`): return the read-only view, unless a
     caller writes to it (check callers; the `.copy()` comment says it was
     added because the arrays were read-only).
   - `_frozen` (`asymmetric.py:173`): return `value` itself when it's an
     array that is already read-only, and set the flag in place when the
     array was just created by the calculation that produced it. Aliasing
     risk: an array a caller still writes to, so keep the copy for arrays
     that come in writable from outside.
   - Upper bound on the saving: about 900 MB of the 1.9 GB (303 + 640 MB).
     Some of the 640 MB is result arrays that are needed anyway, so the real
     figure is lower.
B. **Stop caching `v0`, `v1` and `modesq` on every instance** (530 MB). Either
   compute them on use, or cache them only on the leaves that are reused
   many times. Costs some CPU in `__add__` and `__mul__`; measure the 11 s
   cold time before and after. The todo at `asymmetric.py:369` asks whether to
   store variance instead of the standard deviation; that changes which one
   is derived but not the extra array, so it only helps if `s0` and `s1` are
   dropped instead.
C. **Do not hold per-term spectra the page doesn't show.** Check what the
   spectra page and API use (grouped sums, or per-term values). If only
   groups are shown, accumulate each group while streaming
   `_spectrum_records` and keep the per-term arrays only on request. This
   would make the peak independent of the number of terms, but changes the
   `TermTable` contents, so it needs the consumers read first.
D. **float32 for spectra in memory.** Halves every array. Changes numbers
   slightly (about 7 significant digits), so compare to the float64 result
   before accepting it; keep float64 for the sums.
E. **Check how the stored 587 KB per hit efficiency is split.** The spectra
   hold values, sigma and possibly sigmaup, plus bin edges. If the bin edges
   (or other fields) are identical across hit efficiencies they could be
   stored once. Look at a few stored documents and at `encode_array` (zlib
   flag) before deciding; this changes the stored format, so it is the
   largest and last step.

Measurement plan: after each change run `tm_cdms.py spectra` for the traced
peak (baseline 1.9 GB) and `profile_cdms.py --ops spectra` for time (baseline
11.1 s, RSS +1726 MB), and compare the result to the baseline spectra
numerically.

### Resident memory after a step

RSS stays high after a spike, up to 5 GB after the weights save. With a
process pool in the background-jobs design (`spec §5`, executors), recycle
workers after N jobs; with threads, the web process keeps the peak. Record the
peak of each job kind in its job record so the pool size can be set from data.

## Observations not acted on

- About 40 "Error calculating livetime for HitEfficiency ..." warnings (Pb210
  hit efficiencies such as `Vessel/OuterVacuum surface_in` and `CoolingPipe`)
  are logged during the weights save and the rebuild. They look like existing
  data problems, not part of the memory work.
- Opening the app against this database created an empty `main` version in
  `bexplorer3_testing_cdms`; delete it if it isn't wanted.
- Export takes about six minutes for a 290 MB file, so it is a candidate job
  kind in the background-jobs design.

## Retained memory of the cold spectra table (2026-11-16)

`retained_cdms.py` builds the root's spectra table cold, keeps it, and
reports what it holds (no tracemalloc, so the timing is real). Cold build
11.2 s; RSS peak +2055 MB and +2051 MB after `gc`, so the peak is the
retained table, not transient decoding.

| What | Size |
|---|---|
| 3,466 hit-efficiency blocks, each with all 6 spectrum names (20,796 hit values) | 928 MB of moment arrays (mode, v0, v1) |
| `HitValue.bin_edges`, one array per hit value | 310 MB (13,086 arrays, all with the same content) |
| `_Columns` for one name, stacked copy of the moments | 150 MB (2,110 columns) |
| `tt.nbytes` / `estimate_bytes` | 1,480 / 1,630 MB |

- The spectra cache limit is 154 MB (30% of the default 512), so this table
  is rejected by `ResultsCache`; at the defaults every spectra page load
  repeats the 11 s.
- After the table is dropped RSS is still +1853 MB, and `malloc_trim(0)`
  returns it to +200 MB. So the "RSS stays high after a step" in the note is
  mostly allocator fragmentation from about 60,000 small (25 KB) arrays, not
  live data. Large arrays get their own mappings and are returned on free.
- One name needs 150 MB of columns, about 1/14 of what is held.
