""" What the code of a job can ask of the job it is running in.

Everything here does nothing outside a job, so code that is also called
directly (from a request, a script or a test) needn't know about jobs.
"""
import contextlib
import contextvars
import time
from typing import Optional
from . import models


class JobCancelled(Exception):
    """ The job was asked to stop """


class JobLost(Exception):
    """ The job is no longer leased to this runner, since it was reaped,
    abandoned or finished by someone else. Stop without writing anything """


class Retry(Exception):
    """ Run the job again after `delay` seconds, without using up an
    attempt: it can't proceed yet, for no fault of its own """
    def __init__(self, delay: float = 1.0, message: Optional[str] = None):
        super().__init__(message or f"Try again in {delay:g} s")
        self.delay = delay
        self.message = message


class JobContext:
    """ The state of the job being run by this thread """

    def __init__(self, job_id: str, owner: str, user: Optional[str] = None,
                 progress_interval: float = 0.5, cancel_interval: float = 1.0):
        self.job_id = str(job_id)
        self.owner = owner
        self.user = user
        self.progress_interval = progress_interval
        self.cancel_interval = cancel_interval
        self.clock = time.monotonic
        self._oid = models._oid(job_id)
        self._done = 0
        self._total = None
        self._stage = None
        self._message = None
        self._written = None      # clock reading of the last write
        self._checked = None      # and of the last look at the job

    def _held(self) -> dict:
        return {'_id': self._oid, 'status': 'running',
                'lease.owner': self.owner}

    def progress(self, done, total=None, stage=None, message=None):
        if total is not None:
            self._total = total
        done = max(done, self._done)
        if self._total is not None:
            done = min(done, self._total)
        changed_stage = stage is not None and stage != self._stage
        self._done = done
        if stage is not None:
            self._stage = stage
        if message is not None:
            self._message = message
        now = self.clock()
        final = self._total is not None and done >= self._total
        if not (self._written is None or changed_stage or final
                or now - self._written >= self.progress_interval):
            return
        fields = {'progress.done': self._done}
        for key, value in (('total', self._total), ('stage', self._stage),
                           ('message', self._message)):
            if value is not None:
                fields[f'progress.{key}'] = value
        self._written = now
        if not models.Job._get_collection().update_one(
                self._held(), {'$set': fields}).matched_count:
            raise JobLost(self.job_id)

    def check_cancelled(self):
        now = self.clock()
        if (self._checked is not None
                and now - self._checked < self.cancel_interval):
            return
        self._checked = now
        doc = models.Job._get_collection().find_one(
            {'_id': self._oid},
            {'status': 1, 'cancel_requested': 1, 'lease.owner': 1})
        if (doc is None or doc['status'] != 'running'
                or (doc.get('lease') or {}).get('owner') != self.owner):
            raise JobLost(self.job_id)
        if doc.get('cancel_requested'):
            raise JobCancelled(self.job_id)


_current = contextvars.ContextVar('current_job', default=None)


@contextlib.contextmanager
def running(ctx: JobContext):
    """ Make `ctx` the job that the functions below act on """
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def progress(done, total=None, stage=None, message=None) -> None:
    """ Report that `done` of `total` steps are finished. Writes are
    limited to a few a second, except for a new `stage` and the last step.
    Raises JobLost if the job was taken away from this runner """
    ctx = _current.get()
    if ctx is not None:
        ctx.progress(done, total, stage, message)


def check_cancelled() -> None:
    """ Raise JobCancelled if the job was asked to stop, JobLost if it was
    taken away from this runner. Call it where stopping is safe """
    ctx = _current.get()
    if ctx is not None:
        ctx.check_cancelled()


def current_user() -> Optional[str]:
    ctx = _current.get()
    return None if ctx is None else ctx.user


def current_job_id() -> Optional[str]:
    ctx = _current.get()
    return None if ctx is None else ctx.job_id


def require_idle(scope: str) -> None:
    """ Raise Retry while a serial job other than this one is running in
    `scope`, for jobs that must not start while edits are in progress """
    ctx = _current.get()
    if ctx is None:
        return
    if models.serial_running(scope, exclude=ctx.job_id):
        raise Retry(1.0, 'Waiting for running edits to finish')
