""" Background jobs: a durable queue of work that a pool of workers runs.

This package knows nothing about what the jobs do. Their kinds are
registered by the application with `job_kind`.
"""
from .kinds import JobKind, job_kind, get_kind
from .models import submit, get

__all__ = ['JobKind', 'job_kind', 'get_kind', 'submit', 'get']
