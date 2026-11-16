""" Looking at the jobs that have been recorded, for administration and the
API """
from typing import List, Optional
from . import models


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
