# Background Jobs Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Slow operations (saving a document with its cascade, saving version settings, rebuild, merge, import, delete, export) run as durable background jobs that the web UI and API can watch, and views of a version with a job in progress are visibly flagged as stale.

**Architecture:** A domain-free `bgexplorer.jobs` package (job record, kind registry, progress/cancel context, executors, runner with lease/reaper/heartbeat) is wired into the Flask app by `application/jobs.py` (status page, `submit_and_redirect`, stale-view banner) and `application/job_kinds.py` (the registered kinds, which call the existing domain functions unchanged). Mutual exclusion uses a unique `slot` index plus the existing version locks. The dirty-entry queue (spec section 6, Phase 2) is out of scope.

**Tech Stack:** Python 3, Flask, mongoengine/pymongo, `concurrent.futures`, `unittest` (run with `/home/agent/workspace/.venv/bin/python -m unittest`), MongoDB at 127.0.0.1:27017 via `BGEXPLORER_TEST_MONGODB_URI`.

**Spec:** `docs/superpowers/specs/2026-11-13-background-jobs-design.md` (sections 5, 8, 9.1, 10, 11 and the stale-view section 5.10 added by Task 0).

## Global Constraints

- The `jobs` package imports nothing from `bgexplorer.models`, `bgexplorer.application`, `bgexplorer.versioning`, flask or pint (isolation test, Task 1).
- Existing tests are the regression oracle and run with `JOBS_MODE=eager` (the default when `TESTING`); they must pass unchanged except tests that look at replaced internals.
- Job `params` and `result` are JSON-serialisable only (strings, numbers, bools, lists, dicts, None).
- Progress is written straight to Mongo by whoever runs the job; nothing crosses a process boundary except the job id.
- Validation errors are found synchronously, before a job is enqueued.
- Job statuses in Phase 1: `queued`, `running`, `succeeded`, `failed`, `cancelled`, `abandoned` (`deriving` is Phase 2).
- Lease default 60 s, grace 2.5 s (poll 0.1 s), API wait 100 s, progress interval 0.5 s, keep 7 days, shutdown wait 20 s.
- Never push; commit after each task on branch `feature/background-jobs`.
- Times in the jobs collection are naive UTC.

## Review Focus

- A POST that finishes inside the grace wait must behave exactly as before (same redirect, same flash), including failures that used to re-render the page (409 on a stale merge fingerprint). Pinned in Task 7 and 8.
- An edit submitted while a merge/import holds the version lock must not run half-way: it waits, and is not lost. Pinned in Task 4 (`test_edit_waits_for_lock`).
- A job whose process dies leaves the version usable: the reaper abandons or requeues it, and the stale banner goes away. Pinned in Task 4 and 9.
- Two jobs for one version never run `save_document` at once, and an exclusive job is not starved by a stream of edits. Pinned in Task 4.
- The status page must not become an open redirect (`next=//evil`) or leak other users' jobs. Pinned in Task 6.
- Heartbeat/cancel/progress calls from domain code outside a job are no-ops (existing code paths and tests keep working). Pinned in Task 2.

## File Structure

| File | Responsibility |
|------|----------------|
| `src/bgexplorer/jobs/__init__.py` | Public API: `submit`, `get`, `progress`, `check_cancelled`, `job_kind`, `Retry`, `JobCancelled`, `JobLost`, `configure`, runner access. |
| `src/bgexplorer/jobs/models.py` | `Job` document, `utcnow`, state transitions in one function, queries: `submit`, `claim`, `reap`, `renew`, `finish`, `requeue`, `request_cancel`, `active_for_scope`. |
| `src/bgexplorer/jobs/kinds.py` | `@job_kind` registry (`name`, `serial`, `exclusive`, `idempotent`, `max_attempts`, `retry_on`, `label`). |
| `src/bgexplorer/jobs/context.py` | `ContextVar` of the running job, `progress()`, `check_cancelled()`, `current_user()`, `require_idle(scope)`. |
| `src/bgexplorer/jobs/executors.py` | `EagerExecutor`, `make_executor(mode, workers, initializer, initargs)`. |
| `src/bgexplorer/jobs/runner.py` | `execute(job_id, owner)`, `JobRunner` (dispatcher, reaper, heartbeat, drain, shutdown). |
| `src/bgexplorer/jobs/maintenance.py` | `find_jobs`, `clear_jobs`, `cancel_job`, `abandon_job`. |
| `src/bgexplorer/application/jobs.py` | Blueprint `/jobs` (status page), `init_app`, `submit_and_redirect`, `submit_for_api`, `active_edit_jobs`, process worker initializer. |
| `src/bgexplorer/application/job_kinds.py` | The registered kinds. |
| `src/bgexplorer/application/templates/job.html`, `admin_maintenance.html`, `base.html` | Status page, jobs table, stale banner. |
| `src/bgexplorer/application/{blueprints,versions,admin,api,app}.py`, `cli.py`, `config_default.py` | Wiring. |
| `tests/test_jobs_{model,runner,isolation,app,api,admin,stale}.py`, `tests/jobs_support.py` | Tests (written before each task's code). |

Run one test file: `/home/agent/workspace/.venv/bin/python -m unittest tests.test_jobs_model -v` (export `BGEXPLORER_TEST_MONGODB_URI` first, see memory note).

---

### Task 0: Update the spec

**Files:** Modify `docs/superpowers/specs/2026-11-13-background-jobs-design.md`

- [ ] Add a status paragraph at the top: Phase 1 is being implemented per `docs/superpowers/plans/2026-11-16-background-jobs-phase1.md`; sections 6 and 7 are retained as the Phase 2 design.
- [ ] Add section 5.10 "Stale views": a version has an edit in progress while a job with `scope == tag` is `queued` or `running`. Pages of that version render from what exists (no blocking) with a warning banner (kind label, percent, link to `/jobs/<id>`); API responses carry `X-Bgexplorer-Stale: <job ids>` and `GET /versions/<tag>` has `pending_jobs`. Results computed meanwhile may mix old and new documents; pinning views to the pre-job cache token is future work (section 12). Edits stay allowed and queue.
- [ ] Add `tests/test_jobs_stale.py` to section 9.1, `kinds.py` to the module layout, and list bulk delete/clone as deferred, export as wired.
- [ ] Commit: `git commit -am "Spec: Phase 1 scope, stale views"`.

### Task 1: Job model and store

**Files:** Create `jobs/__init__.py` (empty exports for now), `jobs/models.py`, `jobs/kinds.py`, `tests/test_jobs_model.py`, `tests/test_jobs_isolation.py`.

**Interfaces — Produces:**
```python
# jobs/kinds.py
@dataclass(frozen=True) class JobKind: name, func, serial=False, exclusive=False, idempotent=False, max_attempts=3, retry_on=(), label=None
def job_kind(name, *, serial=False, exclusive=False, idempotent=False, max_attempts=3, retry_on=(), label=None) -> decorator
def get_kind(name) -> JobKind          # KeyError if unknown
# jobs/models.py
STATUSES, ACTIVE = ('queued','running'), FINISHED = ('succeeded','failed','cancelled','abandoned')
def utcnow() -> datetime
class Job(Document): fields per spec 5.1 plus serial, not_before, slot, error_type, label (property)
def submit(kind, params=None, scope=None, user=None, origin=None) -> str   # id; params JSON-checked, kind must be registered
def get(job_id) -> Optional[Job]
def transition(job_id, to, *, expect=None, owner=None, set=None, unset=None, now=None) -> bool   # the single place state changes are made
def claim(owner, lease_seconds, now=None) -> Optional[Job]
def renew(owner, job_ids, lease_seconds) -> list[str]      # ids still held
def reap(now=None) -> list[str]
def finish(job_id, owner, status, result=None, error=None, traceback=None, error_type=None, keep_days=7) -> bool
def requeue(job_id, owner, delay=0, message=None) -> bool   # attempts not counted
def request_cancel(job_id) -> str          # 'cancelled' | 'requested' | 'finished'
def active_for_scope(scope, exclude=None) -> list[dict]     # queued/running jobs
```

- [ ] **Step 1: Failing tests** (`tests/test_jobs_model.py`, using `connect_test_db` and dropping `Job` collection in `setUp`; register throw-away kinds `noop`, `edit` (serial), `excl` (exclusive, idempotent) at module level):
  - `test_submit_defaults`: queued, `created` set, `attempts == 0`, `kind` flags copied (`serial`, `exclusive`).
  - `test_submit_rejects_non_json_params` (ObjectId, set, datetime → `ValueError`) and unknown kind → `KeyError`.
  - `test_transitions`: illegal (`queued->succeeded`, `succeeded->running`) raise `IllegalTransition`; legal ones return True; `expect` mismatch returns False.
  - `test_claim_oldest_first_and_sets_lease`: two jobs, claim returns older, status running, `lease.owner`, `lease.expires` ≈ now+lease, `attempts == 1`; third claim returns None.
  - `test_claim_not_before`: a job with future `not_before` isn't claimed.
  - `test_serial_slot`: two `edit` jobs same scope: first claim ok, second claim returns None while the first runs; a job on another scope is claimed; after `finish` the second is claimable.
  - `test_exclusive_blocks_newer_edits`: queue `edit`(old, running), `excl`, `edit`(new): excl isn't claimable while the old edit runs; the new edit is not claimable while `excl` is queued; after the old edit finishes excl is claimed, and the new edit stays blocked until excl finishes.
  - `test_reap_requeues_idempotent_else_abandons`: expired lease on idempotent job with attempts<max → queued (lease and slot cleared); non-idempotent → abandoned with `error`; unexpired untouched; reap twice doesn't double-handle.
  - `test_renew_only_own_leases`: renewing with another owner returns [] and doesn't extend.
  - `test_finish_requires_owner` and sets `expires_at = finished + keep_days`, clears slot/lease.
  - `test_requeue_does_not_count_attempt`: attempts back to previous, `not_before` in the future, message in progress.
  - `test_request_cancel`: queued → cancelled immediately; running → `cancel_requested` and `'requested'`; finished → `'finished'`.
  - `test_active_for_scope`.
  - `test_indexes`: unique `slot`, TTL on `expires_at`.
  `tests/test_jobs_isolation.py`: copy `tests/test_versioning_isolation.py` with `PACKAGE = .../jobs`, `ALLOWED_EXTERNAL = {'mongoengine','bson','pymongo'}`, and `test_no_domain_words` words `('component','sourceterm','hiteff','version','bgexplorer_')` minus none (the package must not mention versions: `scope` is opaque).
- [ ] **Step 2:** Run both; expected: ImportError / FAIL.
- [ ] **Step 3: Implement.** Key code:

```python
TRANSITIONS = {'queued': {'running', 'cancelled'},
               'running': {'succeeded', 'failed', 'cancelled', 'abandoned', 'queued'}}

def transition(job_id, to, *, expect=None, owner=None, set=None, unset=None, now=None):
    """ Atomically move a job to `to`. Returns False if the job isn't in a state `to` may follow, or (with `owner`) isn't leased by `owner` """
    sources = [s for s, tos in TRANSITIONS.items() if to in tos]
    if expect is not None:
        if to not in TRANSITIONS.get(expect, ()):
            raise IllegalTransition(f"{expect} -> {to}")
        sources = [expect]
    query = {'_id': ObjectId(job_id), 'status': {'$in': sources}}
    if owner is not None:
        query['lease.owner'] = owner
    update = {'$set': dict(set or {}, status=to)}
    if unset:
        update['$unset'] = {k: '' for k in unset}
    return Job._get_collection().update_one(query, update).modified_count == 1
```
  `claim` iterates `find({'status':'queued', '$or':[{'not_before': {'$exists': False}}, {'not_before': {'$lte': now}}]}).sort('created', 1).limit(50)`, applies `_blocked(job)` gating (serial: any exclusive job in scope that is running, or queued with `created` earlier; exclusive: any serial job running in scope or another exclusive running in scope), then `find_one_and_update({'_id', 'status': 'queued'}, {'$set': {status running, lease, started, slot (serial only: f"edit:{scope}")}, '$inc': {'attempts': 1}})`, catching `DuplicateKeyError` (go to next candidate). Serial jobs with no scope use slot `edit:` + id (never conflicts). `reap` is a loop of `find_one_and_update` on `{status: 'running', 'lease.expires': {'$lte': now}}` choosing queued vs abandoned from `get_kind(kind).idempotent` and `attempts < max_attempts` (unknown kind: abandoned). `finish` sets `finished`, `expires_at`, unsets `lease`, `slot`.
- [ ] **Step 4:** Run; PASS. **Step 5:** commit "Add jobs package: job record, claim, lease and reaper".

### Task 2: Context: progress, cancel, user

**Files:** Create `jobs/context.py`, `tests/test_jobs_runner.py` (first part). Modify `jobs/__init__.py`.

**Interfaces — Produces:**
```python
class JobCancelled(Exception); class JobLost(Exception); class Retry(Exception): __init__(self, delay=1.0, message=None)
def progress(done, total=None, stage=None, message=None) -> None   # no-op outside a job
def check_cancelled() -> None                                       # no-op outside a job
def current_user() -> Optional[str]                                 # user of the running job
def current_job_id() -> Optional[str]
def require_idle(scope) -> None      # raises Retry if a serial job other than the current one is running for scope
class JobContext: __init__(job_id, owner, user, progress_interval=0.5, cancel_interval=1.0)
def running(ctx) -> contextmanager that installs ctx in the ContextVar
```

- [ ] **Step 1: Failing tests** (class `TestContext` in `tests/test_jobs_runner.py`): outside a job `progress`, `check_cancelled` return None and `current_user()` is None; inside `running(JobContext(...))` for a claimed job: `progress(1, 10)` is written; calls within the interval are not written but the final (`done == total`) and a stage change are; `done` never decreases and is clamped to `total`; `check_cancelled` raises `JobCancelled` after `request_cancel`; raises `JobLost` when the lease owner changed (job reaped) — both for `progress` and `check_cancelled`; `require_idle` raises `Retry` while another serial job in the scope runs and passes otherwise. Use `interval=0` contexts where timing isn't the subject, and monkeypatch the clock via the `JobContext.clock` attribute (`time.monotonic` default) for the throttle test.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** `progress` builds `$set` of `progress.done/total/stage/message` with query `{_id, status:'running', 'lease.owner': owner}`; `matched_count == 0` → `JobLost`. `check_cancelled` reads `{cancel_requested, status, lease.owner}` at most once per `cancel_interval` (always on first call).
- [ ] **Step 4:** PASS. **Step 5:** commit "Jobs: progress, cancellation and context".

### Task 3: Executors

**Files:** Create `jobs/executors.py`, `tests/jobs_support.py`; extend `tests/test_jobs_runner.py`.

**Interfaces — Produces:**
```python
class EagerExecutor(Executor): submit(fn, *a, **kw) -> Future   # runs inline, exception stored in the Future
def make_executor(mode, workers, initializer=None, initargs=()) -> Executor   # 'eager' | 'thread' | 'process' (spawn context)
```
`tests/jobs_support.py`: `init(uri)` (connects mongoengine, imports the module so kinds register) and kinds `add` / `slow` / `boom` used by the process test.

- [ ] **Step 1: Failing tests**: eager runs inline in the calling thread and captures exceptions; thread mode runs on another thread; `make_executor('bogus', 1)` raises `ValueError`; process mode (marked by `unittest.skipUnless(os.environ.get('BGEXPLORER_TEST_PROCESS', '1') == '1')`) runs `execute` of a `jobs_support` kind in a spawned child and the job reaches `succeeded` (this test is completed in Task 4 once `execute` exists).
- [ ] **Step 2–5:** implement (`ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn'), initializer=initializer, initargs=initargs)`), pass, commit "Jobs: executors".

### Task 4: Runner (`execute`, dispatcher, heartbeat, shutdown, mutual exclusion)

**Files:** Create `jobs/runner.py`; modify `jobs/__init__.py` (export `submit`, `get`, `progress`, `check_cancelled`, `job_kind`, `Retry`, `configure`, `JobRunner`); extend `tests/test_jobs_runner.py`.

**Interfaces — Produces:**
```python
def execute(job_id: str, owner: str, user_scope=None) -> str
    # module-level, picklable. Loads the job, installs the JobContext, calls the kind, writes the final status. Returns the final status.
    # Exceptions: JobCancelled -> cancelled; JobLost -> nothing more is written; Retry or kind.retry_on -> requeue(delay); anything else -> failed (error=str(e) first line, traceback kept, error_type=class name).
    # a returned dict becomes `result`; None -> {}. 
class JobRunner:
    def __init__(self, mode='thread', workers=2, lease_seconds=60, poll_seconds=1.0, shutdown_seconds=20, progress_interval=0.5, initializer=None, initargs=())
    owner: str    # 'host:pid:uuid4-short'
    def start(self) -> None      # thread/process modes: dispatcher + heartbeat threads (idempotent)
    def stop(self, wait=True) -> None   # stop claiming, wait shutdown_seconds, requeue idempotent leftovers, release the rest to the reaper
    def wake(self) -> None
    def drain(self) -> int       # eager: claim and run inline until nothing claimable; returns jobs run
    def submit(self, kind, params=None, scope=None, user=None, origin=None) -> str   # models.submit, then wake() (or drain() in eager)
    def wait(self, job_id, timeout) -> Job   # poll 0.1 s until finished or timeout
    def running_ids(self) -> list[str]
```
User propagation: `configure(user_context=cm_factory)` registers a callable `user -> context manager`; `execute` enters it around the kind (the application passes one that sets the versioning user provider's source; the jobs package only calls it).

- [ ] **Step 1: Failing tests** (class `TestRunner`, kinds defined in the test module with module-level flags/events):
  - eager: `submit` runs inline; `result` stored; failed kind → `failed` with `error`, `error_type`, traceback; `Retry(0, 'waiting')` → back to `queued` with message and attempts unchanged; `retry_on=(KeyError,)` kind behaves the same; `JobCancelled` raised by the kind → `cancelled`.
  - thread mode: `submit` returns immediately while a blocking kind runs (`threading.Event`); progress visible while running; `request_cancel` makes a cooperative kind end `cancelled`; two jobs run concurrently up to `workers`, extra waits.
  - `test_serial_one_per_scope`: two serial jobs same scope with a blocking first: second stays `queued` until the first finishes, then runs; a different scope runs in parallel.
  - `test_exclusive_not_starved`: stream of serial jobs queued after an exclusive job doesn't pass it (uses the Task 1 gating, here end to end).
  - `test_exclusive_waits_for_running_edit_after_lock`: an exclusive kind that calls `require_idle(scope)` while a serial job runs gets requeued and runs once idle.
  - `test_lease_renewal_keeps_job`: lease 0.3 s, job runs 1 s, heartbeat renews, job not reaped; killing the heartbeat (`runner._heartbeat_stop`) lets another runner's reaper requeue an idempotent job / abandon a non-idempotent one, and the original's next `progress` raises `JobLost` (no writes after).
  - `test_stop_requeues_idempotent`: `stop(wait=False)` with a blocked idempotent job leaves it `queued`.
  - `test_user_context`: the factory receives the job's user and `current_user()` returns it in the kind.
  - `test_process_mode` (spawn): enqueue `jobs_support.add`, status `succeeded` with the expected result; `jobs_support.boom` → `failed`.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** Dispatcher loop:
```python
def _loop(self):
    while not self._stopping.is_set():
        try:
            reap()
            while len(self._futures) < self.workers and (job := claim(self.owner, self.lease_seconds)):
                fut = self._executor.submit(execute, str(job.id), self.owner, ...)
                self._futures[str(job.id)] = fut
                fut.add_done_callback(lambda f, i=str(job.id): self._done(i, f))
        except Exception: log.exception(...)
        self._wake.wait(self.poll_seconds); self._wake.clear()
```
  `_done` removes the id, wakes the dispatcher, and if the future raised (e.g. `BrokenProcessPool`) calls `finish(..., 'abandoned', error='worker process died')` and rebuilds the pool. Heartbeat thread: every `lease_seconds/3` call `renew(self.owner, running_ids)`. `execute` computes `owner` mismatch → exits quietly. `eager`: `drain()` loops claim+`execute` until `claim` returns None (jobs with future `not_before` stay queued).
- [ ] **Step 4:** PASS (including process test, which needs `BGEXPLORER_TEST_MONGODB_URI`). **Step 5:** commit "Jobs: runner with leases, reaper, slots and executors".

### Task 5: Thread-safety audit

**Files:** Modify as the audit finds: `models/termcalc.py` (`_factors`), `models/sourceterm.py` (`ResultsCache`, `_current_tokens`), `models/users.py` (`_dummy`); create `tests/test_jobs_threadsafety.py`.

- [ ] **Step 1:** Read each of `termcalc._factors`, every `ResultsCache` instance (`sourceterm.py`, `budget/terms.py`: `_term_cache`, `_table_cache`, `_spectra_cache`), `sourceterm._current_tokens`, `users._dummy`, `verdoc._defined`; record for each: read-only after import / already locked / needs a lock.
- [ ] **Step 2: Failing tests** for each that needs a fix: run 8 threads for 200 iterations hammering `put`/`get`/`purge` of a `ResultsCache` with a barrier start and assert no exception and that `cache_bytes` accounting equals the sum of entries; the same for `_factors` and `_current_tokens` (concurrent calls return equal results). Each test must fail (or be shown to be a race) against the unlocked code; if a structure turns out to be safe already, keep the test as a guard and say why in a one-line comment.
- [ ] **Step 3:** Add `threading.Lock`/`RLock` where needed (keep critical sections to dict operations; never hold across a Mongo call). **Step 4:** PASS and run the whole existing suite. **Step 5:** commit "Make shared calculation caches thread-safe".

### Task 6: Application wiring, status page, `/jobs` blueprint

**Files:** Create `application/jobs.py`, `application/job_kinds.py` (initially empty module with `register()`), `templates/job.html`, `tests/test_jobs_app.py`. Modify `application/app.py`, `application/config_default.py`.

**Interfaces — Produces:**
```python
# application/jobs.py
def init_app(app) -> JobRunner            # reads JOBS_* config, stores app.extensions['jobs'], registers blueprint, before_request lazily starts runner (not in eager mode), atexit/SIGTERM stop
def runner() -> JobRunner                 # the current app's
def submit_and_redirect(kind, params, scope, target) -> Response
    # submit (user = current user name, origin = request.path), wait up to JOBS_GRACE_SECONDS;
    # succeeded -> flash result['messages'] and redirect(result.get('redirect') or target)
    # failed with within-grace -> flash(error, 'danger') and redirect(target)    [callers needing a re-render use submit_and_wait]
    # else 303 to url_for('jobs.status', job_id=, next=target)
def submit_and_wait(kind, params, scope) -> Job   # same wait, returns the Job whatever state
def safe_next(target: str|None) -> str|None       # only same-site absolute paths ('/x', not '//x', no scheme)
def active_edit_jobs(tag) -> list[dict]           # for the banner (Task 9)
def worker_init(uri)                              # ProcessPool initializer: connect, import models and job_kinds, set user hook
```
Config keys (spec 5.8) added to `config_default.py`: `JOBS_MODE=None` (resolved to `eager` when TESTING else `thread`), `JOBS_WORKERS=None` (min(4, cpu)), `JOBS_GRACE_SECONDS=2.5`, `JOBS_API_WAIT_SECONDS=100`, `JOBS_LEASE_SECONDS=60`, `JOBS_POLL_SECONDS=1`, `JOBS_PROGRESS_INTERVAL=0.5`, `JOBS_KEEP_DAYS=7`, `JOBS_SHUTDOWN_SECONDS=20`. The versioning user provider (`auth._entered_by`) is wrapped: if `jobs.current_user()` is set it returns that, else the flask-login user.

Routes: `GET /jobs/<job_id>` (HTML; `?next=`): succeeded → flash messages, 302 to `safe_next(next) or result.redirect or url_for('index')`; failed/cancelled/abandoned → page with error (administrators also see traceback); queued/running → page with progress bar, stage/message, elapsed, a Cancel button (POST `/jobs/<id>/cancel`, then back to the page), polling script against `/api/v1/jobs/<id>` (1 s backing off to 3 s, reload on a finished status) and `<noscript><meta http-equiv=refresh content=3>`. Unknown id or someone else's job (not site_admin, login enabled): 404.

- [ ] **Step 1: Failing tests** (`AppTestCase`-style with `JOBS_MODE='thread'` apps for page tests; eager for the rest): status page for queued/running shows a progress element, the bar width equals percent, `next` is carried into the poll script; succeeded GET flashes and redirects to `next`; `next=//evil.example` and `next=https://evil.example` are ignored (redirect to index); failed shows error and hides the traceback from a non-admin; other user's job → 404, admin sees it; `submit_and_redirect` with a job finishing inside the grace redirects straight to target with the flash (eager), with a slow job (event-blocked, `JOBS_GRACE_SECONDS=0.2`) returns 303 to `/jobs/<id>?next=...`; eager is default under TESTING and thread otherwise (check `app.config` resolution); the runner starts lazily on first request in thread mode and not before.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS; run `tests.test_app` quickly to check nothing else broke. **Step 5:** commit "Jobs: Flask integration and status page".

### Task 7: Wrap edits as jobs (`save_document`, `save_settings`)

**Files:** Modify `application/job_kinds.py`, `application/blueprints.py` (edit handler `edit`, lines ~370-412, `delete`, `clone`), `application/versions.py` (`edit_settings`), `application/api.py` (`update_settings` and the document write endpoints); `tests/test_jobs_app.py`.

Kinds (all `serial=True`, `scope=<version tag>`, `retry_on=(VersionLockedError,)`):
- `save_document` params `{cls, version, objid|None, type, owner, form: {name: [values]}, settings}`: loads or creates the object exactly as the handler does, runs `update_object(obj, MultiDict(form))`, `mark_overrides`, then `owner.add_owned_spec(...)` or `obj.save()`. Returns `{messages: [[f"Successfully saved {obj}", 'success']], redirect: <view url>}` computed with `flask.url_for` inside a `app.test_request_context` created by the kind (`job_kinds` receives the app through `init_app`; in a process pool a throw-away app context is built by `worker_init`). Calls `check_writable(version)` first and `jobs.progress(stage='saving')`.
- `delete_document`, `clone_document` likewise (delete cascades into SourceTerm updates).
- `save_settings` params `{version, form}` replaying `edit_settings` (the slow `add_roi` path); `jobs.progress` per hit efficiency is added in `models/settings.py:_update_rois` as plain `jobs.progress(done, total, 'hit efficiencies')` calls.

Handler change: validation stays synchronous (`update_object` + `obj.validate()` on a throw-away object populate `errors` and re-render 400 as before); only when valid is `submit_and_redirect('save_document', params, scope=version, target=view_url)` returned. The kind re-validates and ends `failed` on a `ValidationError`.

- [ ] **Step 1: Failing tests:** every existing edit/delete/clone/settings test passes in eager mode (run `tests.test_app_components`, `tests.test_app_settings` or the equivalents found by `grep -l "/edit" tests`); new: with `JOBS_MODE='thread'` and `JOBS_GRACE_SECONDS=0.1`, a settings save whose job is held on an event redirects 303 to the status page, then the version is unchanged until released and changed after; form validation errors never create a job (`Job.objects.count() == 0`); an edit POSTed while another process holds `hold_locks([tag])` ends up `queued` with a "waiting" message (retry) and completes after the lock is released (`test_edit_waits_for_lock`); the job's saved document records the requesting user in its history (`current_user_name`) — compare to a synchronous save.
- [ ] **Step 2–5:** implement; PASS; commit "Run document and settings saves as jobs".

### Task 8: Wrap rebuild, merge, import, delete version, export; API `Prefer`

**Files:** Modify `application/job_kinds.py`, `application/admin.py` (`rebuild`), `application/versions.py` (`merge`, `import_file`, `delete`, export), `application/api.py` (merge, import, delete, settings, export endpoints, `/jobs` routes), `tests/test_jobs_app.py`, `tests/test_jobs_api.py`.

Kinds: `rebuild` (serial, `idempotent=False`, progress per component via `jobs.progress` in `sourceterm.rebuild_sourceterms`), `merge`, `import_version`, `delete_version` (all `exclusive=True`, scope = target tag, each takes its own `hold_locks` inside, then `jobs.require_idle(scope)`; `retry_on=(VersionLockedError,)`), `export_version` (plain, scope = tag, writes the archive to `<tmp>/bgexplorer-jobs/<job id>.bgx` and returns `{data: {file: <name>}}`; `GET /jobs/<id>/download` streams it for the job's user; cleaned by `clear_jobs` and when the job expires).
- Upload handling for import: the request saves the upload to `<tmp>/bgexplorer-jobs/<random>.upload`; the job reads and deletes it. Validation of the archive header/format stays synchronous (`import_uploaded_version` split into a validate part and a run part).
- Failure inside grace: `merge` with a stale fingerprint returns the same page with status 409 and the same message as before (handler uses `submit_and_wait`; `MergeError` text is in `job.error`, `error_type == 'MergeError'`).
- API: `POST` merge/import/delete/settings/export call `submit_for_api(kind, params, scope)`: honour `Prefer: respond-async` (202, `Location: /api/v1/jobs/<id>`, `Retry-After: 2`), `Prefer: wait=N`, else wait up to `JOBS_API_WAIT_SECONDS` then 202. A finished job returns exactly the old response body/status (the kind's `result.data` holds the JSON payload; failures map `error_type` to the old status codes via a table in `api.py`).
- `GET /api/v1/jobs/<id>`, `GET /api/v1/jobs?version=&status=&kind=&limit=`, `POST /api/v1/jobs/<id>/cancel` (queued 200, running 202, finished 409), same visibility rule as the status page.

- [ ] **Step 1: Failing tests** (`test_jobs_api.py`): all existing `tests.test_api_*` and version page tests pass in eager mode; new: respond-async gives 202 + Location + Retry-After and the job later `succeeded`; `Prefer: wait=0.1` on a held job gives 202; no header with `JOBS_API_WAIT_SECONDS=0.1` and a held job gives 202; with a fast job the response equals the synchronous one; job JSON shape (`percent` null when `total` null, 50 when 5/10); list filters; cancel statuses; non-owner gets 404; export job produces a downloadable file equal to the synchronous export; merge job vs. a concurrent edit job on the same scope never overlap (assert via events).
- [ ] **Step 2–5:** implement; PASS; commit "Run rebuild, merge, import, delete and export as jobs; async API".

### Task 9: Stale-view banner and header

**Files:** Modify `application/jobs.py` (`active_edit_jobs`), `application/app.py` (`inject_settings`, `after_request`), `application/templates/base.html`, `application/api.py` (version GET gets `pending_jobs`); create `tests/test_jobs_stale.py`.

- [ ] **Step 1: Failing tests** (thread mode, held jobs): while a `save_document` job for `main` is `running`, `GET` of the overview and a component view return 200 with an element `#stale-banner` naming the job kind, percent when known, and a link to `/jobs/<id>`; a view of another version has no banner; after release the banner is gone; a queued job also shows it; the status page and `/jobs` have no banner; API GETs under `/api/v1/versions/main/...` carry `X-Bgexplorer-Stale: <id>` (comma-separated for several) and `GET /api/v1/versions/main` lists `pending_jobs` (`[{id, kind, status, percent}]`); responses of non-GET requests don't get the header; a job abandoned by the reaper stops flagging; the query uses the `(scope, status)` index (assert `explain` winning plan has an IXSCAN).
- [ ] **Step 2: Implement.** `active_edit_jobs(tag)`: `Job._get_collection().find({'scope': tag, 'status': {'$in': ['queued','running']}}, projection)` mapped to `{id, kind, label, status, percent, stage}`. `inject_settings` adds `edit_jobs`. Banner markup in `base.html` inside `#messages`: `<div class="alert alert-warning" id="stale-banner" role="status">{{label}} in progress ({{percent}}%): figures on this page may not include the change yet. <a href="{{ url_for('jobs.status', job_id=...) }}">Watch progress</a></div>`. An `after_request` adds the header when `flask.g.active_version` is set and the method is GET/HEAD.
- [ ] **Step 3–4:** PASS; commit "Flag views of versions with a job in progress".

### Task 10: Admin Jobs section, maintenance module, CLI

**Files:** Create `jobs/maintenance.py`, `tests/test_jobs_admin.py`; modify `application/admin.py`, `templates/admin_maintenance.html`, `cli.py`.

**Interfaces — Produces:**
```python
def find_jobs(status=None, kind=None, scope=None, limit=200) -> list[Job]       # newest first
def cancel_job(job_id) -> str            # as request_cancel
def abandon_job(job_id, reason="marked abandoned by an administrator") -> bool   # running only; never touches any version lock
def clear_jobs(finished_only=True, older_than=None) -> int
```
Admin routes (site_admin only) `POST /admin/jobs/<id>/cancel`, `/admin/jobs/<id>/abandon`, `/admin/jobs/clear`. CLI group `bgexplorer jobs list|cancel ID|clear [--all]` (same `--uri` option as the other groups; entry point added in `pyproject.toml` beside `bgexplorer-versions`).

- [ ] **Step 1: Failing tests:** `find_jobs` filters/order; `abandon_job` on a running job sets `abandoned`, clears slot, the job's next `progress` raises `JobLost`, and a held version lock is untouched (`get_record(tag).active_lock` still set); `clear_jobs` removes only finished jobs and the export files; the admin page lists jobs with status, progress, lease owner and error, the buttons post and redirect, non-admins get 403; CLI via `click.testing.CliRunner`: `list` prints ids and statuses, `cancel`, `clear`.
- [ ] **Step 2–5:** implement; PASS; commit "Admin jobs table and `bgexplorer jobs` CLI".

### Task 11: Docs, benchmark flags, regression, comparison

**Files:** Modify `docs/api.md`, `docs/admin.md`, `docs/cli.md`, config docs; `scripts/profile_standard.py`.

- [ ] Document the job endpoints, `Prefer` behaviour, the `X-Bgexplorer-Stale` header and `pending_jobs`, the status page and stale banner, the admin Jobs section, the CLI, and the `JOBS_*` config keys (table from spec 5.8).
- [ ] `scripts/profile_standard.py`: add `--jobs-mode {inline,thread,process}` and `--workers N`. In `thread`/`process` modes each op's final `save()`/rebuild is run through `JobRunner` (kind registered in the script, params = ids) and the step time is the job's wall clock, peak RSS sampled as today (for the process mode, the child's RSS is also recorded through `jobs` result data `{'peak_rss_mb': ...}` by the kind).
- [ ] Run the whole suite: `/home/agent/workspace/.venv/bin/python -m unittest discover -s tests` (eager default); fix regressions at their cause.
- [ ] Run the comparison: `scripts/profile_standard.py --jobs-mode thread --workers 4` and `--jobs-mode process --workers 4` on the standard scenario for `add_roi,root_weights,rebuild,edit_assay`, and with `profile_cdms.py` on `examples-cdms7tower` for the three slow ops; record a table (time, peak RSS per mode) in `docs/superpowers/notes/2026-11-16-jobs-threads-vs-processes.md`.
- [ ] Commit "Docs, benchmark modes and threads-vs-processes results".

## Self-review

- **Spec coverage:** 5.1 Task 1; 5.2 Tasks 2 and 7/8 (instrumentation); 5.3 Task 4; 5.4 Task 6; 5.5 Tasks 1, 4, 7, 8; 5.6 Task 8; 5.7 Task 10; 5.8 Task 6; 5.9 Tasks 3 and 4; thread-safety audit Task 5; stale views (new 5.10) Task 9; section 9.1 test files map to Tasks 1 to 10; section 10 Task 11. Deferred and stated in Task 0: bulk delete/clone, per-view token pinning, Phase 2.
- **Placeholders:** none intended; Tasks 6 to 10 give interfaces and exact test lists but leave function bodies to the implementer because they replay existing handler code unchanged.
- **Type consistency:** `submit` returns `str` ids everywhere; `Job.id` is converted with `str()`; `execute(job_id, owner, ...)` signature is the one used by the runner and the process test; `Retry(delay, message)` is used by `require_idle` and the kinds; `scope` is the version tag string.
