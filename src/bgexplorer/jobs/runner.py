""" Running jobs: `execute` runs one claimed job and records how it ended;
`JobRunner` claims jobs, hands them to an executor and keeps their leases
alive while they run """
import concurrent.futures as cf
import contextlib
import json
import logging
import os
import socket
import threading
import time
import traceback
import uuid
from concurrent.futures.process import BrokenProcessPool
from typing import Callable, Dict, List, Optional, Tuple
from . import models
from .context import JobCancelled, JobContext, JobLost, Retry, running
from .executors import make_executor
from .kinds import get_kind

log = logging.getLogger('bgexplorer.jobs')

MODES = ('eager', 'thread', 'process')

_DEFAULTS = {
    # called with the user of a job, returns a context manager to run it in
    'user_context': None,
    'progress_interval': 0.5,
    'cancel_interval': 1.0,
    'keep_days': 7,
}
_settings = dict(_DEFAULTS)


def configure(**settings) -> None:
    """ Set how `execute` runs jobs in this process: `user_context`,
    `progress_interval`, `cancel_interval` and `keep_days`. None restores a
    default. Worker processes get the last three from their runner, and
    `user_context` from an initializer that calls this """
    for key, value in settings.items():
        if key not in _DEFAULTS:
            raise TypeError(f"Unknown job setting {key!r}")
        _settings[key] = _DEFAULTS[key] if value is None else value


def _first_line(error: BaseException) -> str:
    lines = str(error).strip().splitlines()
    return lines[0] if lines else type(error).__name__


def _end(job_id, owner, status, **fields) -> str:
    done = models.finish(job_id, owner, status,
                         keep_days=_settings['keep_days'], **fields)
    return status if done else 'lost'


def execute(job_id: str, owner: str) -> str:
    """ Run a job that `owner` has claimed, and record its end. Returns the
    status it was left in: succeeded, failed, cancelled, queued (to be run
    again), or lost if it isn't leased to `owner` (anymore), in which case
    nothing is written """
    job = models.get(job_id)
    if (job is None or job.status != 'running' or job.lease is None
            or job.lease.owner != owner):
        return 'lost'
    ctx = JobContext(job_id, owner, job.user,
                     progress_interval=_settings['progress_interval'],
                     cancel_interval=_settings['cancel_interval'])
    retry_on = ()
    try:
        spec = get_kind(job.kind)
        retry_on = spec.retry_on
        user_context = _settings['user_context']
        with running(ctx), (user_context(job.user) if user_context
                            else contextlib.nullcontext()):
            result = spec.func(json.loads(json.dumps(job.params)))
        result = {} if result is None else result
        if not isinstance(result, dict):
            raise TypeError("A job returns a dict or None, not "
                            f"{type(result).__name__}")
        models._check_json(result, 'result')
    except JobLost:
        return 'lost'
    except JobCancelled:
        return _end(job_id, owner, 'cancelled')
    except Retry as retry:
        return _requeue(job_id, owner, retry.delay, retry.message)
    except Exception as error:
        if isinstance(error, retry_on):
            return _requeue(job_id, owner, 1.0, _first_line(error))
        log.info("Job %s (%s) failed: %s", job_id, job.kind,
                 _first_line(error))
        data = getattr(error, 'job_data', None)
        try:
            models._check_json(data, 'result')
        except (TypeError, ValueError):
            data = None
        return _end(job_id, owner, 'failed', error=_first_line(error),
                    result=data if isinstance(data, dict) else None,
                    error_type=type(error).__name__,
                    traceback=traceback.format_exc())
    return _end(job_id, owner, 'succeeded', result=result)


def _requeue(job_id, owner, delay, message) -> str:
    return 'queued' if models.requeue(job_id, owner, delay, message) \
        else 'lost'


def _init_worker(settings: dict, initializer: Optional[Callable],
                 initargs: Tuple) -> None:
    configure(**settings)
    if initializer is not None:
        initializer(*initargs)


class JobRunner:
    """ Claims jobs and runs them, `workers` at a time. In 'thread' and
    'process' mode this is done by background threads of this process once
    it is `start`ed. In 'eager' mode `submit` runs the job inline, for tests
    and scripts """

    def __init__(self, mode: str = 'thread', workers: int = 2,
                 lease_seconds: float = 60.0, poll_seconds: float = 1.0,
                 shutdown_seconds: float = 20.0,
                 progress_interval: float = 0.5, cancel_interval: float = 1.0,
                 keep_days: float = 7, initializer: Optional[Callable] = None,
                 initargs: Tuple = ()):
        if mode not in MODES:
            raise ValueError(f"Unknown job executor {mode!r}: use "
                             f"{', '.join(MODES)}")
        self.mode = mode
        self.workers = max(1, int(workers))
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.shutdown_seconds = shutdown_seconds
        self.keep_days = keep_days
        self.initializer = initializer
        self.initargs = tuple(initargs)
        self._settings = {'progress_interval': progress_interval,
                          'cancel_interval': cancel_interval,
                          'keep_days': keep_days}
        self.owner = (f"{socket.gethostname()}:{os.getpid()}:"
                      f"{uuid.uuid4().hex[:6]}")
        self._lock = threading.RLock()
        self._futures: Dict[str, cf.Future] = {}
        self._executor: Optional[cf.Executor] = None
        self._broken: List[cf.Executor] = []
        self._threads: List[threading.Thread] = []
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._heartbeat_stop = threading.Event()

    def _new_executor(self) -> cf.Executor:
        return make_executor(self.mode, self.workers, _init_worker,
                             (self._settings, self.initializer,
                              self.initargs))

    @property
    def started(self) -> bool:
        return self._executor is not None

    def start(self) -> None:
        """ Start the executor and, but for eager mode, the threads that
        claim jobs and renew their leases. Does nothing if started """
        with self._lock:
            if self._executor is not None:
                return
            configure(**self._settings)
            self._stopping.clear()
            self._heartbeat_stop.clear()
            self._executor = self._new_executor()
            if self.mode != 'eager':
                self._threads = [
                    threading.Thread(target=self._dispatch, daemon=True,
                                     name='job-dispatcher'),
                    threading.Thread(target=self._heartbeat, daemon=True,
                                     name='job-heartbeat')]
                for thread in self._threads:
                    thread.start()

    def stop(self, wait: bool = True) -> None:
        """ Stop claiming jobs. With `wait`, give the running ones up to
        `shutdown_seconds` to finish. Those left are queued again if their
        kind is idempotent, and otherwise are left to the reaper of whichever
        runner is next, once their lease runs out """
        self._stopping.set()
        self._wake.set()
        dispatcher = self._threads[0] if self._threads else None
        if dispatcher and dispatcher is not threading.current_thread():
            dispatcher.join(5)
        with self._lock:
            pending = list(self._futures.values())
        if wait and pending:
            cf.wait(pending, timeout=self.shutdown_seconds)
        self._heartbeat_stop.set()
        for job_id in self.running_ids():
            self._release(job_id)
        with self._lock:
            executor, self._executor = self._executor, None
            self._threads = []
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _release(self, job_id: str) -> None:
        try:
            job = models.get(job_id)
            if job is not None and get_kind(job.kind).idempotent:
                models.requeue(job_id, self.owner)
        except Exception:
            log.exception("Couldn't release job %s", job_id)

    def wake(self) -> None:
        """ Look for new jobs now rather than at the next poll """
        self._wake.set()

    def running_ids(self) -> List[str]:
        with self._lock:
            return list(self._futures)

    def submit(self, kind: str, params: Optional[dict] = None,
               scope: Optional[str] = None, user: Optional[str] = None,
               origin: Optional[str] = None) -> str:
        """ Queue a job, see `jobs.submit`, and make sure it gets picked up:
        in eager mode it has been run when this returns """
        job_id = models.submit(kind, params, scope, user, origin)
        if self.mode == 'eager':
            self.drain()
        else:
            self.wake()
        return job_id

    def drain(self) -> int:
        """ Run the jobs that can be run now inline, each at most once.
        Returns how many were run. For eager mode """
        self.start()
        ran: List[str] = []
        while True:
            job = models.claim(self.owner, self.lease_seconds, exclude=ran)
            if job is None:
                return len(ran)
            ran.append(str(job.id))
            execute(ran[-1], self.owner)

    def wait(self, job_id: str, timeout: float) -> Optional[models.Job]:
        """ The job, once it has ended or `timeout` seconds have passed """
        deadline = time.monotonic() + timeout
        while True:
            job = models.get(job_id)
            remaining = deadline - time.monotonic()
            if (job is None or job.status in models.FINISHED
                    or remaining <= 0):
                return job
            time.sleep(min(0.05, remaining))

    def _dispatch(self) -> None:
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                self._tick()
            except Exception:
                log.exception("Job dispatcher failed")
            self._wake.wait(self.poll_seconds)

    def _tick(self) -> None:
        self._rebuild_broken()
        models.reap(keep_days=self.keep_days)
        while not self._stopping.is_set():
            with self._lock:
                if len(self._futures) >= self.workers:
                    return
            job = models.claim(self.owner, self.lease_seconds)
            if job is None:
                return
            self._launch(str(job.id))

    def _launch(self, job_id: str) -> None:
        with self._lock:
            executor = self._executor
            if executor is None:
                models.requeue(job_id, self.owner)
                return
            try:
                future = executor.submit(execute, job_id, self.owner)
            except Exception as error:
                log.warning("Couldn't start job %s: %s", job_id, error)
                models.requeue(job_id, self.owner, 1.0, 'Starting again')
                if isinstance(error, BrokenProcessPool):
                    self._rebuild(executor)
                return
            self._futures[job_id] = future
        future.add_done_callback(
            lambda done: self._done(job_id, done, executor))

    def _done(self, job_id: str, future: cf.Future,
              executor: cf.Executor) -> None:
        with self._lock:
            self._futures.pop(job_id, None)
        try:
            error = cf.CancelledError() if future.cancelled() \
                else future.exception()
            if error is not None:
                self._failed(job_id, error, executor)
        except Exception:
            log.exception("Couldn't record the end of job %s", job_id)
        self._wake.set()

    def _failed(self, job_id: str, error: BaseException,
                executor: cf.Executor) -> None:
        """ The executor couldn't run the job to its end, so `execute` didn't
        record one """
        if isinstance(error, (BrokenProcessPool, cf.CancelledError)):
            # this runs in a thread of the broken pool, which can't be shut
            # down from there: leave that to the dispatcher
            with self._lock:
                self._broken.append(executor)
            models.finish(job_id, self.owner, 'abandoned',
                          error='The process running this job died',
                          error_type=type(error).__name__)
        else:
            log.error("Job %s: %r", job_id, error)
            models.finish(job_id, self.owner, 'failed',
                          error=_first_line(error),
                          error_type=type(error).__name__)

    def _rebuild(self, broken: cf.Executor) -> None:
        with self._lock:
            if self._executor is not broken or self._stopping.is_set():
                return
            self._executor = self._new_executor()
        broken.shutdown(wait=False, cancel_futures=True)

    def _rebuild_broken(self) -> None:
        with self._lock:
            broken, self._broken = self._broken, []
        for executor in broken:
            self._rebuild(executor)

    def _heartbeat(self) -> None:
        interval = max(self.lease_seconds / 3, 0.02)
        while not self._heartbeat_stop.wait(interval):
            ids = self.running_ids()
            if not ids:
                continue
            try:
                models.renew(self.owner, ids, self.lease_seconds)
            except Exception:
                log.exception("Couldn't renew job leases")
