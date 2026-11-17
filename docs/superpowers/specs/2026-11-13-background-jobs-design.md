# Background jobs and an incremental derivation queue

Status: Phase 1 (sections 5, 8, 9.1) is being implemented, following
`docs/superpowers/plans/2026-11-16-background-jobs-phase1.md`; sections 6, 7 and
9.2 are kept as the Phase 2 design. Branch `feature/background-jobs`, based on
`worktree-standard-profile-scenario` (the standard scenario and
`scripts/profile_standard.py`).

## 1. Intent

On large models some edits take minutes. The POST that makes the edit holds
a gunicorn thread the whole time, the user sees a hung page, and past the
gunicorn `--timeout 120` the worker is killed mid-write.

The goals, in the order they matter:

1. **Responsiveness.** A POST returns within about a second whatever the
   model size. Work that can't finish in that time continues in the
   background, and the user watches a progress page that continues to the
   page they were going to when it is done.
2. **No lost or inconsistent data.** A crash, restart or kill at any point
   leaves the version repairable, and derived data (SourceTerms) always
   ends up identical to what a from-scratch rebuild would produce.
3. **Less total work.** Recomputing only what changed, once each, in an
   order that avoids redundant recomputation, spread over several workers.
4. **Visibility and control.** Jobs report percent complete through the web
   UI and the API, and an administrator can see, cancel and clear stuck
   ones.

Non-goals: a separate worker container or message broker (a future
upgrade, section 12), changing what is calculated, changing the budget
calculations, or making reads faster.

### Assumptions to confirm

These are my reading of the discussion; each is stated again where it
matters.

- Existing synchronous API clients keep working unchanged. Asynchronous
  behaviour for them is opt-in (`Prefer: respond-async`).
- Today's test suite is the regression oracle. Running it with the job
  runner in `eager` mode (everything inline) must pass unchanged, apart from
  tests that look at internals this spec replaces.
- Worker threads or processes live inside the web processes (gunicorn
  workers). No new deployable.
- Nothing needs to be real-time. A few seconds between an edit and the
  derived data being current is fine as long as the UI says so.

## 2. Where the time goes (baseline)

Standard scenario (`scripts/standard_scenario.py`): 200 assays, 400 leaves,
101 assemblies in 5 levels, 575 placements, 2000 hit efficiencies, 6 ROIs,
16 242 SourceTerms. Measured with `scripts/profile_standard.py --profile`.
Times under cProfile are inflated roughly 2 to 3x and include the harness's
RSS sampling, so they are only for ranking; the targets in section 11 are
checked without `--profile`.

| Operation (one POST)                    | Profiled time |
|-----------------------------------------|---------------|
| view `term_table`, cold                 | 6.2 s         |
| cold spectra                            | 10.1 s        |
| add_roi: save settings                  | 75 s          |
| root_weights: save root, 5 placements   | 34 s          |
| add_leaf: place in one L2 assembly      | 2.5 s         |
| add_leaf: first view after              | 0.3 s         |
| edit_assay: save one assay              | 3.2 s         |
| rebuild_sourceterms                     | 120 s         |
| export version                          | 10.9 s        |
| first `term_table` view after add_roi / root_weights / rebuild | 18.0 / 15.5 / 15.7 s (vs 6.2 s cold on an untouched branch) |

The view after a large edit is two to three times slower than the first
view of an untouched version. Part of that is cProfile overhead on the
cold path, but it is large enough that the derivation leaves the caches
or the SourceTerm documents in a worse state than a plain load; section 7
and the first task of Phase 2 should find out why before a warm job is
designed around it.

Only three operations are slow enough to time out: `rebuild` (120 s),
`add_roi` (75 s, not a cascade) and `root_weights` (34 s). The rest take
seconds, so the grace wait of section 5.4 is what keeps them looking the
same as today.

What the profiles say about where each case spends its time:

- **Saving settings** (a new ROI): about 42 000 `ROI.evaluate` calls
  (hit efficiencies x ROIs, with unit conversion), then a bulk write. This
  isn't the cascade at all; the job should report progress per hit
  efficiency and could later be split across workers (section 12).
- **Saving a root component**: `_sync_placements` and `sync_sourceterms`
  (about a third), `prepare_edit` (copy-on-write of the version's documents,
  about a third), the rest is mongoengine document construction. The
  copy-on-write part is paid once per edit and is not parallelised by the
  dirty queue.
- **Small edits** (add a leaf to one assembly): a few seconds. These already
  feel acceptable; they must not get slower, and their POST should not
  gain a visible detour through a status page (hence the grace wait,
  section 5.4).
- **Not known and must be measured first** (section 11): how much of a
  cascade is `find_parents` queries versus `sync_sourceterms` versus
  hit-efficiency matching. The queue's design doesn't depend on the answer,
  but its worker-count targets do.

## 3. Terms

| Term | Meaning |
|------|---------|
| **Job** | A durable record of one user-visible background operation: kind, parameters, status, progress, result. Phase 1. |
| **Kind** | A registered function that runs a job (`merge`, `import_version`, `rebuild`, `save_document`, ...). |
| **Runner** | The per-process component that claims jobs and entries and runs them on an `Executor`. |
| **Executor** | A `concurrent.futures.Executor`: threads, processes, or `eager` (inline). |
| **Lease** | A claim that expires unless the holder renews it, so a crashed holder's claim frees itself. Same idea as `versioning/locks.py`. |
| **Entry** (dirty entry) | One pending recomputation of the SourceTerms rooted at one component in one version. Phase 2. |
| **Exclusive operation** | One that takes `hold_locks` on a version: merge, import, delete, create-from-version. |
| **Edit** | An ordinary save of a document. Takes no version lock; only calls `check_writable`. |

## 4. Approach and phasing

Two phases, each shippable on its own.

**Phase 1: generic jobs.** A `bgexplorer.jobs` package that knows nothing
about the domain (it must pass the same kind of isolation test as
`versioning`, `tests/test_versioning_isolation.py`): job records, claim,
lease, reaper, progress, executors, and a status page and API. Then the
long-running operations are wrapped in it without changing what they do:
merge, import, rebuild, delete, bulk operations, and "save a document with
its whole cascade". This alone fixes the timeout and the hung page.

**Phase 2: dirty queue.** Replace the recursive `post_save` cascade with
persisted entries processed leaf-first by the worker pool. This is what
makes large edits faster and lets progress be a real percentage. In
Phase 2 the `save_document` job stays, because the save itself is not
always quick (it includes the copy-on-write `prepare_edit`), but it no
longer runs the cascade: it saves, marks entries, and then waits in a
`deriving` state, as a view on those entries, until they are gone.

Rejected alternatives:

- *In-process threads without persistence* (fire and forget). Loses the work
  on restart, can't be seen from another gunicorn worker, no progress API.
- *Celery/RQ with a broker.* Adds a service and a deployment story for a
  tool that today is one container and a Mongo. The `Executor` seam keeps
  that door open (section 12).
- *Making the version lock the lease for edits.* Ordinary edits take no
  lock, and making them take one would serialise every editor on a version.

## 5. Phase 1: jobs

### 5.1 The job record

Collection `jobs`, `Document` in `bgexplorer/jobs/models.py`. Fields:

| Field | Notes |
|-------|-------|
| `kind` | registered name |
| `params` | JSON-serialisable dict, validated on enqueue (no ObjectIds, documents or sets; use strings) |
| `scope` | optional string the runner uses for mutual exclusion, e.g. a version tag. Opaque to the package. |
| `exclusive` | copied from the kind; see 5.5 |
| `user` | name of the requester, for audit and for `current_user_name` in the worker |
| `status` | `queued`, `running`, `deriving` (Phase 2: the kind has returned, derived data is still being computed), `succeeded`, `failed`, `cancelled`, `abandoned` |
| `progress` | `{done, total, stage, message}`; `total` may be null |
| `result` | JSON: `redirect` (relative URL), `messages` ([[text, category], ...] to flash), `data` |
| `error` | short user-facing message; `traceback` kept separately and shown only to administrators |
| `created`, `started`, `finished` | datetimes |
| `lease` | `{owner, expires}`; owner is `host:pid:thread` |
| `attempts`, `max_attempts` | for kinds that are safe to rerun |
| `cancel_requested` | bool |
| `origin` | the requesting URL path, shown on the maintenance page |

Indexes: `(status, created)` for the claim, `(scope, status)`, `lease.expires`
(sparse), `finished` (TTL, `JOBS_KEEP_DAYS`, default 7, applied only to
finished jobs via a separate `expires_at` field set on finish), and a
**unique sparse index on `slot`**, which is the mutual-exclusion primitive
of 5.5.

State transitions are enforced in one function; an illegal transition raises.
Only `queued -> running -> {succeeded, failed, cancelled, abandoned}`,
`running -> deriving -> {succeeded, failed}`, `queued -> cancelled`, and
`running -> queued` (requeue after a lost lease) exist.

### 5.2 Kinds and progress

```python
@job_kind('merge', exclusive=True, idempotent=False)
def run_merge(params): ...
```

A kind is a function taking the params dict and returning the `result` dict.
It reports progress with module-level functions that use a `ContextVar`
holding the current job (no-ops outside a job, so domain code can call them
whether or not it is running in one):

- `jobs.progress(done, total=None, stage=None, message=None)` writes at most
  once per `JOBS_PROGRESS_INTERVAL` (default 0.5 s), except that a change of
  `stage` and the final call are always written. `done` never decreases and
  never exceeds `total`.
- `jobs.check_cancelled()` raises `JobCancelled` when `cancel_requested` is
  set (re-read at most once a second). Called only at points where stopping
  leaves consistent data: between components, never inside a
  `sync_sourceterms` write.

Because progress is written straight to Mongo by whoever runs the job, no
result or progress has to cross a process boundary; this is what lets
thread and process executors behave identically.

Initial instrumentation: `rebuild_sourceterms` (per component),
`_update_sourceterms` (per merged document), version import (per document),
`_update_rois` (per hit efficiency), `save_document` (stage names only until
Phase 2), and bulk operations in `blueprints.py`.

### 5.3 Runner, leases and the reaper

One `JobRunner` per web process, started lazily on first use (a
`before_request` check of a flag) so it survives gunicorn's fork and
`--preload`. A single dispatcher thread loops with a short poll
(`JOBS_POLL_SECONDS`, default 1; a successful enqueue in the same process
wakes it immediately):

1. **Reap**: running jobs whose `lease.expires` is past. If the kind is
   `idempotent` and `attempts < max_attempts`, atomically set back to
   `queued`; otherwise `abandoned`. Both are `find_one_and_update` on
   `{_id, status: running, lease.expires: {$lte: now}}`, so concurrent
   reapers can't double-handle one job.
2. **Claim** up to the number of idle executor slots:
   `find_one_and_update({status: queued, ...claimable}, {$set: running,
   lease, started, $inc attempts})`, oldest first.
3. **Submit** to the executor. The submitted callable is a module-level
   function `execute(job_id)` that loads the job, sets the `current_job`
   and user contextvars, runs the kind, and writes the final status. It
   needs only the id, so it works under a process pool.

A `_Heartbeat`-style thread renews the leases of all jobs this process is
running every `lease/3` (`JOBS_LEASE_SECONDS`, default 60, so a crash frees
its jobs within a minute, unlike the version lock's 5 minutes, which is
deliberately longer). If a renewal finds the lease gone (reaped, or an
administrator marked it abandoned), the running job's next `progress` or
`check_cancelled` call raises `JobLost` and it stops writing.

On clean shutdown (`atexit`, SIGTERM) the dispatcher stops claiming, waits
`JOBS_SHUTDOWN_SECONDS` for running jobs, then releases the rest back to
`queued` if idempotent, else leaves them to the reaper.

### 5.4 The web flow

`jobs.submit(kind, params, scope=None)` enqueues and returns the job id.
`application/jobs.py` (a blueprint at `/jobs`) adds
`submit_and_redirect(kind, params, scope, target)`:

1. Enqueue the job. In `eager` mode it has already run when this returns.
2. Wait up to `JOBS_GRACE_SECONDS` (default 2.5; polling every 0.1 s) for it
   to finish. If it did, flash the job's `result.messages` and redirect to
   `result.redirect or target` exactly as the synchronous code did, so a
   small edit never shows a status page.
3. Otherwise redirect (`303 See Other`, Post/Redirect/Get) to
   `/jobs/<id>?next=<target>`.

The status page `/jobs/<id>` shows the kind, a human description, the
progress bar (indeterminate when `total` is null), the stage, and elapsed
time; polls `GET /api/v1/jobs/<id>` every second, backing off to 3 s; on
`succeeded` reloads itself, and the server-side GET of a succeeded job
flashes the messages and 302s to the target, so the JS needs no logic about
where to go. On `failed` it shows the error and a link back; on
`cancelled`/`abandoned` it says so and offers the version's maintenance
information to administrators. A `<noscript>` meta refresh does the same
without JS. It is only reachable by the job's user and administrators
(others get 404).

Validation errors are still found synchronously, before anything is
enqueued: the edit handler validates the submitted form (it already does)
and re-renders it with errors as today, so a job exists only for input that
should save. If a job's own save raises a `ValidationError` anyway (the data
changed in between), it ends `failed` with the validation message.

### 5.5 Mutual exclusion in Phase 1

Edits take no version lock, and exclusive operations take `hold_locks`.
Jobs must preserve that, and also keep their own safe order:

- **One running `save_document` job per version.** (In Phase 2 it covers
  only the save, since the cascade is no longer inside it, 6.9.) Enforced atomically by the unique sparse `slot` index: on
  claim the runner sets `slot = "edit:<scope>"`; a second claim for the
  same scope hits `DuplicateKeyError` and the dispatcher moves on to
  another candidate. `slot` is unset when the job finishes or is reaped.
  Different versions run in parallel.
- **Shared/exclusive against locks.** Each side registers first, then
  checks the other, so the two orders can't both pass:
  - an edit job is `running` (visible to others) **before** it calls
    `check_writable`, which refuses if a lock is live;
  - an exclusive job takes `hold_locks` **before** it checks that no edit
    job is `running` for the version; if one is, it releases the lock and
    requeues itself with a short delay.
  - while an exclusive job for a version is queued or running, newer edit
    jobs for it aren't claimable (an exclusive job can't be starved).
- A job refused by a live lock goes back to `queued` without counting an
  attempt, and the status page says what it is waiting for
  (`message`: "waiting for the merge into 'v1' to finish").

### 5.6 API

Under `/api/v1` (check the existing prefix in `application/api.py`; match
it):

- `GET /jobs/<id>` returns `{id, kind, status, progress: {done, total,
  percent, stage, message}, result, error, created, started, finished}`.
  `percent` is null when `total` is null.
- `GET /jobs?version=&status=&kind=&limit=` lists the caller's jobs
  (administrators: all).
- `POST /jobs/<id>/cancel`: queued jobs cancel immediately; running ones set
  `cancel_requested` and report `202`.
- Mutating endpoints that can be slow (`/versions/<tag>/merge`, `/import`,
  and the document endpoints) follow RFC 7240: with `Prefer:
  respond-async` they return `202 Accepted` with `Location: /api/v1/jobs/<id>`
  and `Retry-After`; with `Prefer: wait=N` they wait up to N seconds. With no
  `Prefer` header they behave as today (block until the job finishes), but
  after `JOBS_API_WAIT_SECONDS` (default 100, under the gunicorn timeout)
  they too return `202` + `Location` rather than being killed. This is the
  only change visible to existing clients and it only occurs for requests
  that previously risked a timeout.
- `docs/api.md` documents all of this.

### 5.7 Maintenance page

`application/admin.py` gains a "Jobs" section beside locks: a table (id,
kind, scope, user, status, progress, age, lease owner and expiry, error),
and actions: cancel, mark abandoned (for a running job whose owner is gone,
available once the lease is expired or after confirmation), delete finished.
`jobs.maintenance` has `find_jobs` / `clear_jobs` mirroring
`versioning/maintenance.find_locks` / `clear_locks`, and the CLI gets
`bgexplorer jobs list|cancel|clear`. Abandoning or clearing a job never
clears a version lock; that stays a separate, explicit action, since a lock
means "this version may be half-written".

### 5.8 Configuration

All in the Flask config with environment overrides (as the existing
settings do):

| Key | Default | Meaning |
|-----|---------|---------|
| `JOBS_MODE` | `thread` | `eager`, `thread` or `process`. `TESTING` defaults to `eager`. |
| `JOBS_WORKERS` | `min(4, cpu_count)` | executor size per web process |
| `JOBS_GRACE_SECONDS` | `2.5` | web wait before showing the status page |
| `JOBS_API_WAIT_SECONDS` | `100` | |
| `JOBS_LEASE_SECONDS` | `60` | |
| `JOBS_POLL_SECONDS` | `1` | |
| `JOBS_PROGRESS_INTERVAL` | `0.5` | |
| `JOBS_KEEP_DAYS` | `7` | |
| `JOBS_SHUTDOWN_SECONDS` | `20` | |

Gunicorn stays at `--workers 2 --threads 4`. Executor threads are extra
to the request threads, so total threads per process are 4 + `JOBS_WORKERS`
+ 2 (dispatcher, heartbeat). Both gunicorn workers run a dispatcher and
share one queue through Mongo, so the effective pool is
2 x `JOBS_WORKERS`.

### 5.9 Executors

`jobs.executors.make_executor(mode, workers, config)`:

- `eager`: an `Executor` subclass whose `submit` runs the callable
  immediately and returns a completed `Future`. The dispatcher is not
  started; `submit_and_redirect` drains inline. Tests use this.
- `thread`: `ThreadPoolExecutor`. Shares the process's `ResultsCache`s,
  Mongo connection and imported modules.
- `process`: `ProcessPoolExecutor` with the `spawn` multiprocessing context
  (a forked pymongo client is unsafe, and the web process has threads) and
  an `initializer` that reconnects mongoengine from the app's URI and
  imports the model modules so signals and registrations exist. Tasks are
  module-level functions taking JSON params or ids only.

Context propagation, which the code relies on and which neither pool does
by itself: the `held_locks` contextvar and the user provider
(`versioning.verdoc.current_user_name`). A job carries its `user`; the
`execute` wrapper installs it. An exclusive kind takes its own
`hold_locks` inside `execute` (it must not inherit a lock from the
request, which has already returned). A thread-safety audit of
module-level mutable state is a Phase 1 task: `models/termcalc.py:_factors`,
the `ResultsCache` instances, `sourceterm._current_tokens`,
`models/users.py:_dummy`, and `verdoc._defined` (read-only after import).

### 5.10 Stale views

A version has an *edit in progress* while any job with `scope == tag` is
`queued` or `running`. Reads never block on it: pages and API responses are
built from what exists, and are flagged.

- **Pages** show a warning banner (`#stale-banner`) with the job's label,
  its percent when known, and a link to `/jobs/<id>`: "figures on this page
  may not include the change yet".
- **API** responses for the version (GET/HEAD) carry
  `X-Bgexplorer-Stale: <job ids, comma separated>`, and `GET
  /versions/<tag>` lists `pending_jobs` (`id`, `kind`, `status`,
  `percent`).
- The flag is derived from the job collection (one indexed query by
  `(scope, status)`), so a job abandoned by the reaper or an administrator
  stops flagging with no cleanup. The jobs pages themselves have no
  banner.
- Edits stay allowed while a job is pending; they queue behind it
  (5.5).
- Honest limit: a job saves documents one at a time and each save replaces
  the version's cache token, so a view computed while the job runs may mix
  old and new documents, and is computed cold. Pinning views to the token
  from before the job (serving the previous consistent results from cache)
  is future work (section 12).

## 6. Phase 2: the dirty queue

### 6.1 Why

Today `update_component` and `update_parent` find what changed and
recompute it in the same recursive walk, inside the save, in one thread,
rewalking parents for every placement. The walk's *shape* (leaf-first over
the ancestors of the edited component) is cheap to compute and the
*recomputations* (one `sync_sourceterms` per affected component) are the
expensive part. Separating them makes the recomputation a set of
independent, idempotent, individually retryable tasks that can be ordered,
deduplicated, skipped when nothing changed, and spread over workers.

### 6.2 Invariant

Every component's SourceTerms in a version equal what
`rebuild_sourceterms` would produce, **once the version's dirty queue is
empty**. While entries remain, SourceTerms may be stale.

Two rules make this hold under concurrency and crashes without any
cross-entry transaction:

1. *Every change to an input of X bumps X's entry generation.* The inputs of
   component X are X's own document (and its specs) and the SourceTerms of
   its children.
2. *An entry is removed only if its generation at completion equals its
   generation when it was claimed* (compare-and-delete).

Ordering (children before parents) is an optimisation that avoids recomputing
a parent twice. It is not needed for correctness, because a parent
recomputed too early is re-queued by rule 1 when its child later changes.

### 6.3 Entry record

Collection `derive_entries`, one per `(version, target)`, unique index on
that pair:

| Field | Notes |
|-------|-------|
| `version`, `target` | version tag and `original_id` of the component |
| `generation` | int, incremented on every mark |
| `cause` | `content` (the component or its specs changed; its result propagates to parents) or `rematch` (hit-efficiency matching only; independent of ordering, doesn't propagate) |
| `self_dirty` | the component itself was marked, as opposed to being only an ancestor of one that was |
| `children_changed` | a child finished with changed SourceTerms since this entry last ran |
| `child_targets` | the target ids of its direct children, stored at mark time for the claim query |
| `state` | `queued`, `running`, `failed` |
| `claim` | `{owner, expires, generation}` while running; same lease and heartbeat as jobs |
| `attempts`, `error` | |
| `origins` | job ids waiting for this entry, `$addToSet` on mark |

Indexes: `(version, target)` unique, `(version, state)`, `target`,
`claim.expires` (sparse), `origins`.

### 6.4 Marking

`derive.mark_dirty(version, targets, cause, origin_job=None)`:

1. Upsert an entry for each target: `$inc generation`, `$set self_dirty`,
   `$addToSet origins`, state `queued` unless currently `running` (a running
   entry keeps `running` and its bumped generation makes its completion
   re-queue it).
2. Walk upward with `find_parents` (indexed on `children.component`),
   breadth-first, from each target, and create an entry for every ancestor
   that lacks one (`self_dirty` false, generation 1, `queued`). Existing
   ancestor entries are left alone: their generation is bumped by the
   completion of a child that changed (6.6), not here, and an ancestor that
   is running and finishes before then is recreated by that same
   completion. Only `content` causes walk upward.
3. Store `child_targets` from each assembly's current `children`.

The set of ancestors is the *upper bound* of the work and is what the job's
progress total is based on; ancestors that turn out not to need
recomputation count as done the moment they are skipped (6.6), so the
progress bar moves quickly past them. The hierarchy level is **not** used;
the `Assembly.clean` TODO notes it goes stale, and a marked entry's order
comes only from `child_targets`.

The post-save signals no longer recurse. `update_component`,
`update_assembly`, `update_emissionspec` (components that own the spec),
`update_hiteff` (components whose SourceTerms match, found with
`distinct('assemblyRoot')`, `cause=rematch`; replaces re-saving each
SourceTerm), `update_activatedmaterial` and `delete_component` only mark.
Marking happens after the document has been saved (as the signals are
today); a crash between the save and the mark loses the mark. That window
is microseconds wide and `rebuild_sourceterms` (kept, and kept as the
repair tool on the maintenance page) restores consistency. A cheap
`derive.verify(version)` that compares SourceTerm counts per root against
a fresh candidate generation is a possible later addition, not part of this
spec.

### 6.5 Claiming

A claim is two steps; the first can be stale, the second is atomic:

1. Find candidates: `state: queued`, version not exclusively locked by
   someone else, oldest `_id` first (ancestors are created after the
   entries they depend on, so this is roughly leaf-first). A candidate is *ready* if no entry exists with `target` in its
   `child_targets` and `state` in (`queued`, `running`, `failed`). One
   `count` query per candidate, for a bounded number of candidates per
   pass.
2. `find_one_and_update({_id, state: queued, generation: g}, {$set: running,
   claim, $inc attempts})`. Failing the match just means someone else
   claimed it or it was re-marked; try the next candidate.

If no candidate is ready but some are queued (all blocked), the pass ends
and retries after the poll interval; a cycle can't exist (Assembly.clean
rejects circular placements), but the runner guards by forcing the entry
with the fewest blockers after a timeout and logging it.

Entries in a version with a live lock held by another context are skipped
without counting an attempt.

### 6.6 Running an entry

`derive_entry(entry_id, generation)` runs in the executor:

1. If `not self_dirty and not children_changed`, the entry has nothing to
   do (early cutoff): go to step 4 with `changed = False` and without
   calling `sync_sourceterms`.
2. Otherwise recompute: leaf components call
   `update_component(..., propagate=False)`'s sync; assemblies call
   `update_assembly(..., propagate=False)`'s `_sync_placements` over all
   placements. Both already exist on the profile branch (the `propagate`
   parameter). Neither calls `settings.touch`; it is done once, see 6.8.
   `sync_sourceterms` already compares content digests, so
   `result.changed` is exact.
3. If `changed` and the entry's cause is `content`, for each parent (from
   `find_parents`) atomically `$set children_changed: true, $inc
   generation` and set `queued` on the parent's entry, creating it if it
   doesn't exist (a parent can lose its entry between marking and now).
4. Compare-and-delete: `delete_one({_id, generation: <claimed>})`. If it
   deleted nothing, the entry was re-marked while running; set it `queued`
   and clear the claim, and it runs again. In either case, remove this
   entry's `origins` from jobs' progress accounting (6.9).
5. If this deleted the last entry of the version, perform the drain actions
   (6.8).

Errors: a `ValidationError` or unit error from `sync_sourceterms` (which
raises *before* writing anything) is permanent: state `failed`, error
stored, not retried. Database and other transient errors retry up to 3
times with the lease expiring between tries. A `failed` entry blocks its
ancestors (6.5 treats `failed` as active), so a parent never computes from a
stale child; unaffected branches keep going.

### 6.7 Scope restriction

`update_parent` today restricts the sync to SourceTerms along the changed
placement paths (`scope`), which can be much cheaper than syncing the whole
assembly when one of many placements changed. This spec **does not** carry
that over: entries are per component and sync all placements. If section
11's measurements show assemblies with many placements dominate, a
follow-up adds an optional `scope` (union of placement ids) to the entry.

### 6.8 Cache tokens and the pending state

Every `touch` replaces the version's cache token and, unless `terms_only`,
the calc token, and that makes `ResultsCache` entries unreachable. Touching
per entry (as `update_component`/`update_assembly` do now) would defeat the
cache for the whole drain. Instead:

- Entries don't call `touch`. `settings.touch(version, terms_only=True)` is
  called **once, when the version's last entry is deleted** (and, for
  `rematch`/hit-efficiency or ROI changes that already touch non-terms-only
  in their own handlers, as they do now). A second touch from a racing
  last-completer is harmless.
- While any entry exists for a version, views of it are served but marked
  stale: a banner on the version's pages, "Updating: N of M components
  recomputed" with the percent from the entry counts, links to the progress
  page of the user's job if they have one. `ResultsCache` results computed
  during the drain are stored under the token current at that time and are
  discarded by the final touch, so a stale result can't outlive the drain.
- `can_edit` stays true during a drain (edits keep queueing); it is only
  false for a live lock, as today.

### 6.9 Jobs over entries

The edit POST is the Phase 1 flow: validate, then `submit_and_redirect`
a `save_document` job. Its kind function applies the form, calls
`obj.save()`, and the post-save mark creates or bumps entries with the job
as `origin`; it then returns, and the runner sets the job to `deriving`
instead of `succeeded` if any entry lists it in `origins`. From then on
the job's progress is computed from its entries (total = entries it
originated at mark time; done = those no longer listing it); the job
becomes `succeeded` when the last entry removes it from `origins` (done by
the same step that deletes an entry, 6.6 step 4), and `failed` if any of its
entries is `failed`. With the grace wait, a small edit's entries drain
before the redirect and the user sees the result directly. Two edits
sharing a dirty ancestor both list their jobs in its `origins` and both
finish when it does.

The dispatcher claims and runs entries; a `deriving` job is only the
handle for the UI and API. Job leases and reaping apply to the job while it
is `running` (the save), and not once it is `deriving`; entry claims and
their own reaper take over. Since the save no longer includes the cascade
the slot mutex of 5.5 is held only for the save, and is released when the
job becomes `deriving`.

### 6.10 Exclusive operations

Merge, import, delete and create-from-version first take `hold_locks`, then
require that the version(s) they read and write have **no entries**:

- if entries exist, the operation fails fast with a clear "N updates pending
  in 'v1', try again shortly" (HTTP 409 in the API), or, for web requests,
  waits up to the grace period for the queue to drain, since the user's own
  preceding edit is the usual cause;
- once the lock is held no new entries can be created by others (their
  saves are refused by `check_writable`) and the runner skips locked
  versions, so the queue can only drain, and the operation waits for that;
- the operation's own derivations (`after_merge`, `after_import`, which may
  raise to undo the merge) call `derive.run_inline(version)`, which
  marks and then processes entries in the calling thread and context,
  as the lock holder, so the undo semantics are unchanged.
  Distributing a merge's derivation across the pool (a nested submit
  would deadlock a full pool) is a possible later optimisation.
- Entries are idempotent descriptions of "recompute from the current data",
  so entries left behind by a failed or undone operation are harmless: they
  run once the lock is released or cleared and recompute whatever the data
  now is.

`delete_version` removes the version's entries. `create_branch` from a
version with entries is refused with the same message as above, since a
branch shares documents with its source until edited and would otherwise
inherit a half-derived state with no entries.

### 6.11 Progress and visibility

`GET /api/v1/versions/<tag>/pending` returns `{entries, running, failed,
percent}`; the version page banner polls it. The maintenance page lists
`failed` entries (component name, error), with "retry" (set `queued`) and
"rebuild this version" (existing). Failed entries also show on the user's
job page and in the version banner.

## 7. Cache pre-warming (future, designed for)

Derivation computes SourceTerm values (emission rates and live times) but
not budget results, and the drain's final touch replaces the cache token, so
the first view after a large edit is cold (6 s for `term_table` in the
baseline; 10 s for spectra). A `warm` job kind (not part of this spec's
implementation) would, after a drain and optionally at server start,
compute `term_table`, `table` and `dashboard` for each root component of
the versions in use. It is only worthwhile with the `thread` executor
(processes don't share the web processes' caches), and with processes it
would have to run in the web process. The comparison in section 10 measures
how much of the cold view cost the derivation already pays for free (via
shared `ResultsCache` hits in `sync_sourceterms`), which decides whether
this is worth building.

## 8. Module layout

```
src/bgexplorer/jobs/            # no domain imports (isolation-tested)
    __init__.py                 # submit, progress, check_cancelled, job_kind
    models.py                   # Job document, transitions
    runner.py                   # dispatcher, claim, reaper, heartbeat
    executors.py                # eager/thread/process
    kinds.py                    # @job_kind registry
    context.py                  # current_job contextvar, progress, cancel, user hook
    maintenance.py              # find_jobs, clear_jobs
src/bgexplorer/models/derive.py # DirtyEntry, mark_dirty, claim, derive_entry, run_inline
src/bgexplorer/models/signals.py# handlers mark instead of recurse; rebuild stays
src/bgexplorer/application/jobs.py   # /jobs blueprint, submit_and_redirect
src/bgexplorer/application/...       # edit handlers, versions.py, api.py, admin.py use it
```

`jobs` depends only on mongoengine, pymongo and the standard library; the
application layer registers kinds (`jobs.job_kind`) from domain code, and
a `user_provider` hook is passed in the way `versioning` takes
`set_user_provider`.

## 9. Tests to write first

All use real MongoDB through `tests/dbutil.connect_test_db`, like
`tests/test_lock_leases.py`; tests that spawn processes need
`BGEXPLORER_TEST_MONGODB_URI` and skip without it. "Eager" tests run with
`JOBS_MODE=eager`, "pool" tests are parametrised over `thread` and
`process`. Time is controlled by patching the module's `now` (as
`test_lock_leases` does with `record_module.now`) and by short
`JOBS_*` intervals, never by sleeping for real intervals longer than 0.2 s.

### 9.1 Phase 1

`tests/test_jobs_model.py`

- enqueue stores kind, params, scope, user, `queued`; non-JSON params are
  rejected; unknown kind is rejected.
- legal transitions succeed; every illegal one raises (parametrised over the
  full status matrix).
- finished jobs get `expires_at` and are removed by retention; running and
  queued never.

`tests/test_jobs_runner.py`

- *claim atomicity*: 8 threads race to claim 20 queued jobs; each is claimed
  exactly once, in `created` order per thread.
- *slot mutex*: two `save_document` jobs for one scope never run
  concurrently (assert max concurrency 1 with a barrier in the kind);
  different scopes do run concurrently.
- *exclusive vs edit*: with an edit job running, an exclusive job for the
  same scope isn't started and is requeued after taking and releasing its
  lock; while an exclusive job is queued first, a later edit job isn't
  claimed (no starvation); edit job refused by a live lock returns to
  `queued` with attempts unchanged.
- *shared/exclusive race*: loop 200 times starting an edit job and an
  exclusive job together from two threads; never both inside their critical
  sections.
- *lease*: heartbeat extends `lease.expires`; a job whose lease is expired
  is requeued if idempotent and under `max_attempts`, abandoned otherwise;
  one with a live lease is untouched; two reapers racing handle each expired
  job once.
- *lost lease*: renewal failure makes the next `progress` raise `JobLost`
  and the job writes nothing further.
- *progress*: no-op outside a job; at most one write per interval (count
  writes with 1000 calls in a tight loop); stage change and final call
  always written; `done` monotonic and clamped to `total`; `total` None gives
  `percent` None.
- *cancel*: queued cancels immediately; running stops at the next
  `check_cancelled`, ends `cancelled`, and a held version lock is released;
  cancel of a finished job is a no-op reporting its status.
- *graceful shutdown*: running jobs given time to finish; unfinished
  idempotent ones return to `queued`.
- *pool parity* (thread/process, eager as the reference): same final status,
  `result` and error handling; an exception becomes `failed` with a message
  and a traceback only in `traceback`; progress written inside the worker is
  seen by a second connection; `current_user_name()` inside the kind equals
  `job.user` and documents saved there record it; the process runs in a
  different pid (process only); a worker killed with SIGKILL mid-job is
  reaped within the lease and requeued.

`tests/test_jobs_isolation.py` (after `test_versioning_isolation.py`)

- importing `bgexplorer.jobs` and everything under it imports no module of
  `bgexplorer.models`, `bgexplorer.application`, `bgexplorer.versioning`.

`tests/test_jobs_app.py`

- fast job within grace: the POST redirects straight to the target with the
  same flashed message as today; **all existing `test_app_*` edit tests pass
  unchanged in eager mode** (a CI job runs the whole suite this way).
- slow job (kind patched to wait on an event): POST returns 303 to
  `/jobs/<id>?next=...`; the page renders a progress bar with the stored
  percent; when released, GET `/jobs/<id>` flashes and redirects to the
  target; failed job renders the error and a link back, no traceback;
  traceback visible to administrators only.
- a form with validation errors re-renders with 400/200 as today and
  creates no job.
- access: another user gets 404 for a job; administrators see it.
- merge, import-version and rebuild run as jobs and end in the same
  database state as the synchronous versions (compare with the old code
  path on a copy); merge failure still undoes and leaves the abandoned lock
  of `abandon_lock`.
- instrumentation: `rebuild` job's `done` reaches `total`.

`tests/test_jobs_api.py`

- `Prefer: respond-async` gives 202 with `Location` and `Retry-After`; the
  job URL gives the documented JSON; no `Prefer` blocks and returns what
  `test_api_versions` expects today; a job exceeding `JOBS_API_WAIT_SECONDS`
  returns 202 instead of timing out; `wait=N` honoured.
- list filters by version, status and kind; users see their own only;
  cancel works and returns 202 for running, 200 for queued; unknown id 404.
- authorisation matches the existing API rules.

`tests/test_jobs_stale.py`

- while a job for a version is queued or running its pages show the banner
  (kind, percent, link), other versions' don't; API GETs carry
  `X-Bgexplorer-Stale`; the version GET lists `pending_jobs`; the flag goes
  away when the job finishes or is abandoned; the query uses the
  `(scope, status)` index.

`tests/test_jobs_admin.py`

- the maintenance page lists jobs with progress, lease and error; cancel,
  mark-abandoned and delete-finished work and are admin-only (403
  otherwise); none of them touches a version lock; the CLI commands do the
  same.

### 9.2 Phase 2

`tests/test_derive_equivalence.py` (the main safety net)

- *golden*: for each scenario below, apply the edit, drain, and compare the
  version's SourceTerms (by `natural_key` and `stamp`) with those from a
  fresh `rebuild_sourceterms` on a copy, and with the current recursive
  implementation (kept in the test module as the reference until it is
  removed). Scenarios: edit a leaf's spec rate; change a placement weight;
  add and remove a placement; delete a component with parents; attach and
  detach a spec; edit a spec shared by several components; hit-efficiency
  add, edit (changed matching fields) and delete; ROI change; a leaf in two
  assemblies (diamond); one assembly placed several times; 5-level nest;
  an edit that changes nothing derivable.
- *random sequences*: seeded random series of 30 edits drawn from the above
  on a 40-component hierarchy, draining after random subsets of them; final
  state equals a rebuild. Seeds are fixed and printed on failure.

`tests/test_derive_queue.py`

- *dedup*: marking one target 100 times leaves one entry with generation
  100 (single thread) and exactly one entry with 8 threads marking
  concurrently (unique index holds, no `DuplicateKeyError` escapes).
- *ancestors*: marking a leaf creates entries for all ancestors (not
  siblings or descendants) and `child_targets` match `children`; a diamond's
  parent has one entry.
- *ordering*: in a chain leaf, A, B, root, `claim` returns only the leaf
  first, then A, and so on; a diamond parent isn't claimable until both
  children are gone; with all of a version blocked by a `failed` leaf
  nothing above it is claimable but other branches are.
- *early cutoff*: with `sync_sourceterms` mocked to count calls, an edit
  that changes the leaf's SourceTerms recomputes every ancestor once; an
  edit whose SourceTerms are unchanged recomputes the leaf only and ancestors
  complete without calling `sync_sourceterms`; a diamond recomputes the
  parent once.
- *generation*: a mark arriving while the entry runs (injected at a hook
  inside `derive_entry`) re-queues it and it runs again; no mark is lost;
  completion never deletes an entry with a newer generation.
- *crash*: a claimed entry whose claim has expired is claimed again and
  completes; the final state equals a rebuild; an entry claimed by a live
  lease isn't taken.
- *failure*: a unit error in one leaf marks only that entry `failed`, its
  ancestors stay queued and are not computed, siblings complete; after the
  data is fixed and the entry retried, all complete; permanent errors are
  not retried, transient ones are (3 times); the failure appears in the
  pending API and the maintenance page.
- *locks*: entries in a version locked by another context are skipped
  without attempts; `run_inline` as the holder processes them; a merge
  whose `version_merged` receiver raises is undone and leaves no entries;
  `delete_version` removes entries; `create_branch` and merge refuse (or
  wait, for web) while entries exist; with the lock held the queue only
  drains.
- *touch*: `settings.touch` is called once per drain (mock count), not per
  entry; during a drain the pending API reports entries and percent, and
  `ResultsCache` content stored during the drain isn't served after it.
- *jobs over entries*: a `deriving` job's percent equals the share of its
  entries gone; entries skipped by early cutoff count; two jobs sharing an
  ancestor both succeed when it completes; job `failed` if any of its
  entries failed.

`tests/test_derive_pool.py`

- *parity*: the equivalence scenarios under `thread` and `process` produce
  the same SourceTerms as eager.
- *stress*: 8 threads make random concurrent edits on a 40-component
  hierarchy; drain with 4 workers; compare with a rebuild. Repeated 20 times.
- *thread safety*: derive disjoint components concurrently under a
  `ThreadPoolExecutor` and sequentially; results identical; guards the audit
  of module-level state (5.9).
- *process init*: the `spawn` initializer connects, imports models and runs
  a derive; two processes can claim concurrently without double-processing
  an entry (count executions).

### 9.3 Existing tests to adapt

Tests that assert on the side effects of `update_component`/`update_parent`
immediately after `save()` (for example in `test_sourceterm.py`,
`test_budget.py`, `test_app_hiteff.py`) need a `derive.drain(version)` after
the save when run outside eager mode. In eager mode the signal handlers
drain inline, so they pass unchanged; that is the compatibility test for
the signals change, and the `signals.py` refactor must not require editing
those tests beyond this.

## 10. Comparing threads and processes

Not a unit test; a benchmark after Phase 2 works, run with the harness and
recorded in `docs/` (or the spec's follow-up) with the machine's CPU count.

The harness gets: `--jobs-mode eager|thread|process`, `--workers N`, and
for each op two measurements, *time to response* (the POST returns) and
*time to consistent* (drain complete), plus the first `view` after. Also
total CPU time (user+sys over wall time, summed over children for
processes) as the parallel-efficiency figure, and peak RSS including
children. A small `scripts/compare_profiles.py` prints two or more
`--json` outputs side by side.

Matrix: ops `add_leaf`, `edit_assay`, `root_weights`, `add_roi`, `rebuild`;
modes eager, thread, process; workers 1, 2, 4, 8 (up to the machine's
cores). Each cell is the median of 3 fresh runs, without `--profile`.

Hypotheses to test (the user's, plus mine): threads win on the first view
after a drain, because derivation populates the shared `ResultsCache`;
thread speed-up is limited by the GIL for the pure-Python parts of
`sync_sourceterms` and mongoengine document construction, which the cProfile
shows are a large share, so processes may win on throughput despite cold
caches and N copies of the cache memory. The decision rule: pick the mode
with the lowest *time to consistent* for the two worst ops
(`root_weights`, `rebuild`) provided the first-view time after is no more
than 20% worse and peak RSS is under the container's limit; otherwise
choose threads. The default `JOBS_MODE` is set from the result, and the
result is kept in the docs.

## 11. Success criteria

Measured on the standard scenario, without `--profile`, on the same machine
as the baseline, and recorded back into the table in section 2:

1. Every POST returns (with a redirect, either to the target or to a status
   page) in under 3 seconds at the 95th percentile, for every op in the
   harness, in `thread` mode with default settings.
2. `add_leaf` placed in one L2 assembly: time to consistent no worse than
   today's (about 2.5 s profiled).
3. `root_weights` and `rebuild`: time to consistent at least 3x faster
   than the baseline at 4 workers (a target to confirm in section 10, not a
   promise: depends on how much of the cost is the unparallelisable
   `prepare_edit`). `edit_assay` and `add_leaf` no worse than today.
4. After any sequence of edits and drains, SourceTerms equal a rebuild
   (section 9.2 equivalence tests).
5. No test in the existing suite changes except as described in 9.3.
6. The standard-scenario `rebuild` is not slower than today.
7. A running job can be seen, its percent updates at least every 2 seconds
   when `total` is known, and a SIGKILLed worker's job is recovered or
   abandoned within `JOBS_LEASE_SECONDS`.

Before Phase 2 starts, add instrumentation (timers around `find_parents`,
`sync_sourceterms`, hit-efficiency matching) to answer the open question in
section 2 and record the breakdown here.

## 12. Future work

- **Separate worker container.** The runner's claim/execute loop is
  independent of the web app: a `bgexplorer worker` command that starts a
  dispatcher with `JOBS_MODE=process|thread` and no web server, using the
  same image and Mongo. The web processes then run with `JOBS_WORKERS=0`.
  Only deployment config and the CLI command change.
- **Cache pre-warming**, section 7.
- **Pinned stale views**: while a job runs, serve views from the caches
  keyed to the token from before it started, so they are consistent
  (old) rather than a mix, and cold views don't recompute from half-written
  data.
- **Bulk delete/clone** (`/bulk/delete`, `/bulk/clone`) as jobs; they are
  fetch calls returning JSON and need client-side handling of `202`.
- **Splitting a single heavy operation across workers**: ROI evaluation per
  hit efficiency (the 75 s `add_roi`), bulk imports per document, once the
  pool exists. The entry mechanism isn't the right shape for these;
  they'd be job kinds that submit child jobs.
- **Scoped entries** (6.7).
- **Server-sent events** instead of polling for the status page.
- **Consistency check** (`derive.verify`), to detect a lost mark without
  a rebuild.

## 13. Risks and open questions

- *Thread safety of the model code.* The audit in 5.9 could find
  non-thread-safe state (the term calculators, caches). If the cost of
  fixing it outweighs `thread` mode's benefits, `process` becomes the
  default and pre-warming (section 7) is dropped.
- *Process pool memory.* Each process has its own `ResultsCache` limited by
  `BGEXPLORER_CACHE_MB`; N workers per gunicorn worker multiply memory. The
  initializer should lower the cache budget for pool processes.
- *The post-save to mark window* (6.4) loses a mark on a crash in that
  window; `rebuild` repairs it. Accepted.
- *`deriving` jobs have no lease of their own* (6.9); a stuck entry is found
  by its claim lease, so the job page for a stuck derivation shows
  "waiting" until the entry is reaped. The maintenance page shows the
  entry.
- *Behaviour change visible to users:* after an edit that doesn't finish
  within the grace period, the version page shows stale derived values
  with a banner instead of the user waiting for correct ones. This is the
  point of the change but is a user-visible semantic change to confirm.
