""" Background jobs in the web application: the runner of each app, the
status page, and `submit_and_redirect` for handlers that hand their work to a
job. The kinds of job are in `job_kinds` """
import atexit
import logging
import os
import signal
import threading
from typing import List, Optional
import flask
from flask_login import current_user
from .. import jobs as core
from ..jobs import models
from ..models.users import Role
from ..versioning.verdoc import current_user_name
from . import auth

log = logging.getLogger(__name__)

# the app that jobs build their urls with. This is the last one created, which
# is the only one in a server; worker processes make their own in `worker_init`
_app: Optional[flask.Flask] = None


def job_app() -> flask.Flask:
    """ The application that code running in a job can use for `url_for` """
    if _app is None:
        raise RuntimeError("The job application hasn't been created")
    return _app


def runner(app: Optional[flask.Flask] = None) -> core.JobRunner:
    """ The job runner of `app`, or of the current app """
    return (app or flask.current_app).extensions['jobs']


def safe_next(target: Optional[str]) -> Optional[str]:
    """ `target` if it is a path on this site, otherwise None """
    return target if auth.is_local_path(target) else None


def worker_init(uri: str) -> None:
    """ Initializer of the processes of a process pool: connect to the
    database and build an app, so that kinds are registered and can make
    urls and record who made a change """
    global _app
    from .app import create_app
    _app = create_app(config={'MONGODB_URI': uri, 'JOBS_MODE': 'eager',
                              'LOGIN_DISABLED': True, 'SECRET_KEY': 'unused'})


def _resolve(config) -> None:
    mode = config.get('JOBS_MODE') or ('eager' if config.get('TESTING')
                                       else 'thread')
    config['JOBS_MODE'] = mode
    if not config.get('JOBS_WORKERS'):
        config['JOBS_WORKERS'] = min(4, os.cpu_count() or 1)


def _stop_on_sigterm(job_runner: core.JobRunner) -> None:
    """ Let running jobs finish when the server is stopped with SIGTERM, if
    nothing else (such as gunicorn) deals with it """
    if (threading.current_thread() is not threading.main_thread()
            or signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL):
        return

    def terminate(signum, frame):
        job_runner.stop()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    signal.signal(signal.SIGTERM, terminate)


def init_app(app: flask.Flask) -> core.JobRunner:
    """ Create the job runner of `app` from its JOBS_* configuration and add
    the status page. Jobs are only run once the first request arrives, or in
    eager mode, when they are submitted """
    global _app
    from . import job_kinds   # noqa: F401  registers the kinds
    config = app.config
    _resolve(config)
    mode = config['JOBS_MODE']
    job_runner = core.JobRunner(
        mode=mode, workers=config['JOBS_WORKERS'],
        lease_seconds=config['JOBS_LEASE_SECONDS'],
        poll_seconds=config['JOBS_POLL_SECONDS'],
        shutdown_seconds=config['JOBS_SHUTDOWN_SECONDS'],
        progress_interval=config['JOBS_PROGRESS_INTERVAL'],
        keep_days=config['JOBS_KEEP_DAYS'],
        initializer=worker_init if mode == 'process' else None,
        initargs=(config['MONGODB_URI'],))
    app.extensions['jobs'] = job_runner
    _app = app
    app.register_blueprint(create_blueprint(), url_prefix='/jobs')

    if mode != 'eager':
        @app.before_request
        def start_jobs():
            if not job_runner.started:
                job_runner.start()
        atexit.register(job_runner.stop)
        if not config.get('TESTING'):
            _stop_on_sigterm(job_runner)
    return job_runner


# ---- submitting jobs from requests

def submit_and_wait(kind: str, params: dict, scope: Optional[str]
                    ) -> models.Job:
    """ Submit a job for the current user, and wait up to JOBS_GRACE_SECONDS
    for it to end. Returns the job, whatever state it is in """
    job_runner = runner()
    job_id = job_runner.submit(kind, params, scope=scope,
                               user=current_user_name(),
                               origin=flask.request.path)
    return (job_runner.wait(job_id, flask.current_app.config[
        'JOBS_GRACE_SECONDS']) or models.get(job_id))


def flash_result(job: models.Job) -> None:
    """ Show the messages a job returned """
    for message, category in (job.result or {}).get('messages') or ():
        flask.flash(message, category)


def submit_and_redirect(kind: str, params: dict, scope: Optional[str],
                        target: str) -> flask.Response:
    """ Run a job as a request would do its work directly: if it ends within
    the grace period, show its messages and redirect to `target` (or where
    the job says), and if it fails, show the error and go to `target` all the
    same. If it takes longer, redirect to its status page, which takes the
    user on to `target` when it is done """
    job = submit_and_wait(kind, params, scope)
    if job.status == 'succeeded':
        flash_result(job)
        return flask.redirect(safe_next((job.result or {}).get('redirect'))
                              or target)
    if job.status in models.FINISHED:
        flask.flash(_failure(job), 'danger')
        return flask.redirect(target)
    return flask.redirect(flask.url_for('jobs.status', job_id=job.id,
                                        next=target), 303)


def _failure(job: models.Job) -> str:
    if job.status == 'failed':
        return job.error or "The job failed"
    if job.status == 'cancelled':
        return f"{job.label} was cancelled"
    return f"{job.label} was abandoned: {job.error or 'it did not finish'}"


# ---- showing jobs

def may_see(job: models.Job) -> bool:
    """ Whether the current user may look at, and cancel, `job`: it is theirs,
    or they are an administrator """
    if auth.has_role(Role.site_admin):
        return True
    return bool(current_user.is_authenticated and job.user is not None
                and job.user == current_user.name)


def job_json(job: models.Job) -> dict:
    """ A job as the status page and the API show it """
    def when(time):
        return None if time is None else time.isoformat() + 'Z'
    finished = job.status in models.FINISHED
    progress = job.progress
    return {
        'id': str(job.id), 'kind': job.kind, 'label': job.label,
        'status': job.status, 'finished': finished, 'percent': job.percent,
        'done': progress.done, 'total': progress.total,
        'stage': progress.stage, 'message': progress.message,
        'error': job.error, 'error_type': job.error_type,
        'user': job.user, 'scope': job.scope,
        'cancel_requested': job.cancel_requested,
        'created_at': when(job.created), 'started_at': when(job.started),
        'finished_at': when(job.finished),
        'result': job.result if finished else None}


def duration_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def active_edit_jobs(tag: str) -> List[dict]:
    """ The jobs queued or running for version `tag` """
    return models.active_for_scope(tag)


def create_blueprint() -> flask.Blueprint:
    bp = flask.Blueprint('jobs', __name__)

    def visible_job(job_id: str) -> models.Job:
        job = core.get(job_id)
        if job is None or not may_see(job):
            flask.abort(404, "There is no such job")
        return job

    @bp.get('/<job_id>')
    def status(job_id):
        job = visible_job(job_id)
        target = safe_next(flask.request.args.get('next'))
        if job.status == 'succeeded':
            flash_result(job)
            return flask.redirect(
                target or safe_next((job.result or {}).get('redirect'))
                or flask.url_for('index'))
        since = job.started or job.created
        return flask.render_template(
            'job.html', job=job, next=target,
            active=job.status in models.ACTIVE,
            show_traceback=auth.has_role(Role.site_admin),
            elapsed=duration_text(
                (models.utcnow() - since).total_seconds()))

    @bp.get('/<job_id>/status')
    def status_json(job_id):
        return flask.jsonify(job_json(visible_job(job_id)))

    @bp.post('/<job_id>/cancel')
    def cancel(job_id):
        job = visible_job(job_id)
        if (response := auth.require(Role.editor)) is not None:
            return response
        outcome = models.request_cancel(job.id)
        if outcome == 'cancelled':
            flask.flash(f"{job.label} was cancelled", 'info')
        elif outcome == 'requested':
            flask.flash(f"Asked {job.label} to stop; it will at the next "
                        "safe point", 'info')
        return flask.redirect(flask.url_for(
            'jobs.status', job_id=job.id,
            next=safe_next(flask.request.args.get('next'))))

    return bp
