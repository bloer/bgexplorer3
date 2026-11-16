""" Background jobs: a durable queue of work that a pool of workers runs.

This package knows nothing about what the jobs do. Their kinds are
registered by the application with `job_kind`.
"""
from .kinds import JobKind, job_kind, get_kind
from .models import submit, get
from .context import (JobCancelled, JobLost, Retry, progress, check_cancelled,
                      current_user, current_job_id, require_idle)

__all__ = ['JobKind', 'job_kind', 'get_kind', 'submit', 'get', 'JobCancelled',
           'JobLost', 'Retry', 'progress', 'check_cancelled', 'current_user',
           'current_job_id', 'require_idle']
