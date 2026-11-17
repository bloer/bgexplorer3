""" Looking at the jobs that have been recorded and clearing them up, for
administration and the API """
import datetime
import os
import tempfile
from typing import List, Optional
from . import models


def default_files_dir() -> str:
    """ Where jobs keep the files they take and make, unless configured """
    return os.path.join(tempfile.gettempdir(), 'bgexplorer-jobs')


def find_jobs(status: Optional[str] = None, kind: Optional[str] = None,
              scope: Optional[str] = None, user: Optional[str] = None,
              limit: int = 200) -> List[models.Job]:
    """ The most recent jobs, newest first, optionally only those with the
    given `status`, `kind`, `scope` and `user` """
    if status is not None and status not in models.STATUSES:
        raise ValueError(f"Unknown job status {status!r}")
    query = {name: value for name, value in (
        ('status', status), ('kind', kind), ('scope', scope), ('user', user))
        if value is not None}
    return list(models.Job.objects(**query).order_by('-created', '-id')
                .limit(limit))


def cancel_job(job_id) -> Optional[str]:
    """ Cancel a queued job, or ask a running one to stop at its next safe
    point. Returns 'cancelled', 'requested', 'finished' if it had already
    ended, or None if there is no such job """
    return models.request_cancel(job_id)


def abandon_job(job_id, reason: str = "Abandoned by an administrator"
                ) -> bool:
    """ Give up on a running job that doesn't stop, e.g. because it hangs:
    it is recorded as abandoned and no longer holds its scope, and whatever
    runs it finds out the next time it reports progress. The job's own work
    isn't undone and nothing else it holds is released. False if the job
    isn't running """
    now = models.utcnow()
    return models.transition(
        job_id, 'abandoned', expect='running', unset=('lease', 'slot'),
        set=dict(models._finish_fields(now, 7), error=reason))


def clear_jobs(finished_only: bool = True,
               older_than: Optional[datetime.timedelta] = None,
               files_dir: Optional[str] = None) -> List[str]:
    """ Delete the records of jobs that have finished, or with
    `finished_only=False` those that are waiting too; running jobs are never
    deleted. With `older_than`, only those that ended (or were queued) longer
    ago than that. The files in `files_dir` that belong to them, which are
    named by their id, are removed as well. Returns the ids deleted """
    if finished_only:
        query = {'status': {'$in': list(models.FINISHED)}}
    else:
        query = {'status': {'$ne': 'running'}}
    if older_than is not None:
        cutoff = models.utcnow() - older_than
        query['$or'] = [{'finished': {'$lt': cutoff}},
                        {'finished': None, 'created': {'$lt': cutoff}}]
    coll = models.Job._get_collection()
    oids = [doc['_id'] for doc in coll.find(query, {'_id': 1})]
    if oids:
        # a job may have started since it was looked at
        coll.delete_many({'_id': {'$in': oids},
                          'status': {'$ne': 'running'}})
    ids = [str(oid) for oid in oids]
    if files_dir and ids:
        _remove_files(files_dir, set(ids))
    return ids


def _remove_files(folder: str, job_ids) -> None:
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.split('.', 1)[0] in job_ids:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
