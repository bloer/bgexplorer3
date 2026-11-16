# Background jobs: threads or processes?

Measured on 2026-11-16 with the `bgexplorer.jobs` runner of phase 1
(branch `feature/background-jobs`), as the spec asked: the same operations run
inline (as before), as a job of a pool of threads, and as a job of a pool of
worker processes (`spawn`, 4 workers). One run of each, one at a time on an
otherwise idle machine, so differences under about 10% are noise.

How to repeat:

```
scripts/profile_standard.py --jobs-mode inline|thread|process --workers 4 [--probe]
    --ops add_roi,root_weights,rebuild,edit_assay        # the standard scenario
docs/superpowers/notes/2026-11-14-cdms-memory-profile/profile_cdms.py
    --jobs-mode ... --ops add_roi,rebuild,edit_assay      # examples-cdms7tower
```

Each operation is made on a fresh branch. The views before the edit run in the
parent process, then the change and the save or rebuild it needs run as one
job, then the views after run in the parent again. "Job" below is the wall
clock from submitting the job to its end. In process mode the worker clears its
results cache before it starts, so it begins as a fresh worker would. `--probe`
requests the table and dashboard of the branch from a thread of the parent
every 0.25 s while the job runs (the stale views of phase 1), and reports the
latency of those requests.

## Time of the jobs

| Model | Operation | Inline | Thread | Process |
|-------|-----------|-------:|-------:|--------:|
| standard | add_roi (save settings) | 30.5 s | 34.3 s | 32.9 s |
| standard | root_weights (save root) | 16.6 s | 18.1 s | 16.6 s |
| standard | rebuild | 60.7 s | 70.3 s | 62.2 s |
| standard | edit_assay | 2.5 s | 2.5 s | 3.2 s |
| CDMS | add_roi (save settings) | 168 s | 181 s | 159 s |
| CDMS | rebuild | 134 s | 150 s | 135 s |
| CDMS | edit_assay | 76 s | 86 s | 73 s |

- The overhead of the job machinery itself (submit, claim, progress, result)
  is about 0.2 s (thread, edit_assay: 3.43 s as a job against 3.22 s inside it).
- Process mode costs about 2.2 s more than the work for the first job after
  the pool starts, to spawn the worker and build the application (32.9 s as a
  job against 30.7 s inside it, for the first `add_roi`; 4.9 s against 2.6 s
  for `edit_assay` on its own). The later jobs cost 0.2-0.9 s more, with
  the cache cleared. All of that is small against jobs that take minutes.
- Thread mode is 8-16% slower than inline on the long jobs in this run, and
  process mode is within 6% of inline (faster on CDMS). The parent is idle
  while it waits, so this is a guess rather than a finding: the job's thread
  has to share the GIL with the runner's own threads (poll, lease renewal,
  progress). It should be measured again before it is acted on.

## Memory

Peak resident size, in MB. For the process mode, the worker and the parent are
separate processes.

| Model | Operation | Inline | Thread (one process) | Process: parent | Process: worker |
|-------|-----------|-------:|---------------------:|----------------:|----------------:|
| standard | add_roi | 257 | 264 | 233 | 216 |
| standard | rebuild | 338 | 344 | 244 | 324 |
| CDMS | add_roi | 626 | 710 | 630 | 279 |
| CDMS | rebuild | 1182 | 1280 | 630 | 1169 |
| CDMS | edit_assay | 699 | 792 | 545 | 690 |

The process worker's figure is the peak of the whole worker, which keeps
whatever the earlier jobs left, so it can only be larger than the job's.

- With threads, the server process takes the job's peak on top of what it holds
  for the views: 1.3 GB for a CDMS rebuild, and more than that if the pages are
  warm. With processes, the parent stays at 630 MB and the 1.17 GB is in the
  worker, which gives it back when it ends or is recycled. Total memory is
  about the same, or higher for processes since the worker has a cache of its
  own, but the web processes stay small, which is what gunicorn's limits and
  the OOM killer look at.
- With 4 workers and a rebuild each, the worst case is 4 x 1.2 GB for CDMS in
  either mode. `JOBS_WORKERS` is the way to bound it.

## What the pages see while a job runs

The `--probe` requests, which are what the stale views of phase 1 serve, for
the table and dashboard of the version being edited:

| Model | Operation | Mode | Requests | Median | 95th percentile | Slowest |
|-------|-----------|------|---------:|-------:|----------------:|--------:|
| standard | add_roi | thread | 57 | 117 ms | 362 ms | 26.3 s |
| standard | add_roi | process | 97 | 17 ms | 20 ms | 7.3 s |
| standard | rebuild | thread | 224 | 29 ms | 228 ms | 0.46 s |
| standard | rebuild | process | 230 | 15 ms | 19 ms | 0.02 s |
| CDMS | add_roi | thread | 675 | 20 ms | 34 ms | 12.4 s |
| CDMS | add_roi | process | 561 | 16 ms | 21 ms | 9.3 s |
| CDMS | rebuild | thread | 401 | 99 ms | 236 ms | 10.7 s |
| CDMS | rebuild | process | 502 | 15 ms | 19 ms | 0.02 s |

- With processes, warm views are not affected by the job at all: the median
  is 15-17 ms and the 95th percentile 19-21 ms in every run.
- With threads, the job and the pages share the GIL: in the standard
  add_roi and both rebuilds, warm views take 2 to 8 times as long while the job
  runs (median 29-117 ms instead of 15 ms, 95th percentile 228-362 ms). That is
  slower but still responsive, and not a stall. The CDMS add_roi is hardly
  affected (median 20 ms), presumably because it spends most of its time
  waiting for the database, not computing.
- Every `add_roi` run has one request that took 7-26 s, in both modes. With
  processes, only `add_roi` has it; with threads the CDMS rebuild has a
  10.7 s one too, which may be a different cause (GIL contention
  while the job swaps its results in). For `add_roi`, most likely it is
  the first view after the job saves the new ROI settings, which recalculates
  the table for the new settings from the old results, and is the same cost
  as a cold view (6-9 s). This is not investigated; if it is so, it is
  worth a look as part of the phase 2 work on stale views, since it makes the
  "stale" view of an `add_roi` as slow as an uncached one. With threads the
  one recalculation is 1.3-3.6 times slower than with processes.

## What this does not show

- The probe asks for one version with a warm cache. Several users viewing many
  pages and versions would compete with a thread job for the GIL more.
- The benefit that was expected of threads, that the job reuses the cache
  that the views warmed, doesn't show in these operations: the edits
  invalidate most of what the views calculated (the cheap `edit_assay` is the
  one operation that keeps its terms, and it isn't faster in thread mode,
  2.5 s against 3.2 s with a cold worker, a difference that is just the
  cold start). It would matter for jobs that read much that they don't change,
  such as an export or a merge preview; they weren't measured.
- A worker process starts with no cache, so the first views it calculates are
  cold. Pre-warming is the future work that would change this.
- One run of each, as said above.

## Recommendation

Use `process` where the server has the memory for the workers, and `thread`
(the default) otherwise. Processes keep the pages' latency at its idle value
and keep the job's memory out of the web processes. They cost a few seconds
for the first job after a restart and a second copy of the cache, which are
small against jobs that take minutes. Threads are acceptable with the default
of a few workers, since the pages stay responsive (tens of milliseconds to a
fraction of a second) but they take the job's memory into the web process.
The setting is `FLASK_JOBS_MODE`, and which to use by default is for the
admin, not the code, to decide.
