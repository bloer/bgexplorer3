""" The job record, and the queries that move jobs through their states.

Every change of status is made by `transition`, with a query that only
matches if the job is in a state it may leave, so that two processes can't
both move the same job. Times are UTC without a time zone, as MongoDB
stores them.
"""
import datetime
from typing import List, Optional
from bson import ObjectId
from bson.errors import InvalidId
from mongoengine import (Document, EmbeddedDocument, EmbeddedDocumentField,
                         StringField, IntField, BooleanField, DateTimeField,
                         DictField)
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from .kinds import get_kind

ACTIVE = ('queued', 'running')
FINISHED = ('succeeded', 'failed', 'cancelled', 'abandoned')
STATUSES = ACTIVE + FINISHED

TRANSITIONS = {
    'queued': {'running', 'cancelled'},
    'running': {'succeeded', 'failed', 'cancelled', 'abandoned', 'queued'},
}

# a candidate list of this length is enough to find a job that isn't blocked
CLAIM_CANDIDATES = 50


class IllegalTransition(ValueError):
    """ Asked for a change of status that no job can make """


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class Progress(EmbeddedDocument):
    done = IntField(default=0)
    total = IntField()
    stage = StringField()
    message = StringField()


class Lease(EmbeddedDocument):
    owner = StringField(required=True)
    expires = DateTimeField(required=True)


class Job(Document):
    kind = StringField(required=True)
    params = DictField()
    # opaque to this package: jobs with the same scope may exclude each other
    scope = StringField()
    serial = BooleanField(default=False)
    exclusive = BooleanField(default=False)
    user = StringField()
    # where to go back to, e.g. the page that submitted the job
    origin = StringField()
    status = StringField(required=True, choices=STATUSES, default='queued')
    progress = EmbeddedDocumentField(Progress, default=Progress)
    result = DictField()
    error = StringField()
    error_type = StringField()
    traceback = StringField()
    created = DateTimeField(required=True, default=utcnow)
    started = DateTimeField()
    finished = DateTimeField()
    # delay claiming until this time
    not_before = DateTimeField()
    lease = EmbeddedDocumentField(Lease)
    attempts = IntField(default=0)
    max_attempts = IntField(default=3)
    cancel_requested = BooleanField(default=False)
    # the job is deleted at this time
    expires_at = DateTimeField()
    # held while a serial job runs; the unique index admits one per scope
    slot = StringField()

    meta = {
        'collection': 'jobs',
        'indexes': [
            [('status', 1), ('created', 1)],
            [('scope', 1), ('status', 1)],
            {'fields': ['lease.expires'], 'sparse': True},
            {'fields': ['expires_at'], 'expireAfterSeconds': 0},
            {'fields': ['slot'], 'unique': True, 'sparse': True},
        ],
    }

    @property
    def percent(self) -> Optional[int]:
        total = self.progress.total
        if not total:
            return None
        return int(round(100 * (self.progress.done or 0) / total))

    @property
    def label(self) -> str:
        try:
            return get_kind(self.kind).label or self.kind
        except KeyError:
            return self.kind


def _check_json(value, path='params'):
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check_json(item, f'{path}[{i}]')
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} has a key that isn't a string: "
                                 f"{key!r}")
            _check_json(item, f'{path}[{key!r}]')
    else:
        raise ValueError(f"{path} isn't JSON data: {type(value).__name__}")


def _oid(job_id) -> Optional[ObjectId]:
    try:
        return ObjectId(str(job_id))
    except (InvalidId, TypeError):
        return None


def submit(kind: str, params: Optional[dict] = None,
           scope: Optional[str] = None, user: Optional[str] = None,
           origin: Optional[str] = None) -> str:
    """ Queue a job, returning its id. Raises KeyError for an unknown kind
    and ValueError if `params` isn't JSON data """
    spec = get_kind(kind)
    params = {} if params is None else params
    _check_json(params)
    job = Job(kind=kind, params=params, scope=scope, user=user,
              origin=origin, serial=spec.serial, exclusive=spec.exclusive,
              max_attempts=spec.max_attempts)
    job.save()
    return str(job.id)


def get(job_id) -> Optional[Job]:
    oid = _oid(job_id)
    return None if oid is None else Job.objects(id=oid).first()


def transition(job_id, to: str, *, expect: Optional[str] = None,
               owner: Optional[str] = None, set: Optional[dict] = None,
               unset=(), inc: Optional[dict] = None) -> bool:
    """ Atomically move a job to status `to`, applying the field changes.
    Returns False if the job isn't in a status that `to` can follow (or is
    not in `expect`), or, with `owner`, isn't leased by `owner`. Raises
    IllegalTransition if `expect` is a status that can't go to `to` """
    sources = [s for s, tos in TRANSITIONS.items() if to in tos]
    if expect is not None:
        if to not in TRANSITIONS.get(expect, ()):
            raise IllegalTransition(f"A job can't go from {expect} to {to}")
        sources = [expect]
    oid = _oid(job_id)
    if oid is None:
        return False
    query = {'_id': oid, 'status': {'$in': sources}}
    if owner is not None:
        query['lease.owner'] = owner
    update = {'$set': dict(set or {}, status=to)}
    if unset:
        update['$unset'] = {key: '' for key in unset}
    if inc:
        update['$inc'] = dict(inc)
    return Job._get_collection().update_one(query, update).modified_count == 1


def _older(doc) -> dict:
    """ Query for jobs submitted before `doc` """
    return {'$or': [{'created': {'$lt': doc['created']}},
                    {'created': doc['created'], '_id': {'$lt': doc['_id']}}]}


def _blocked(doc) -> bool:
    """ Whether the exclusion rules keep this job from starting now """
    scope = doc.get('scope')
    if scope is None or not (doc.get('serial') or doc.get('exclusive')):
        return False
    coll = Job._get_collection()
    if doc.get('exclusive'):
        query = {'scope': scope, 'status': 'running',
                 '$or': [{'serial': True}, {'exclusive': True}]}
    else:
        query = {'scope': scope, 'exclusive': True, '$or': [
            {'status': 'running'},
            {'status': 'queued', **_older(doc)}]}
    return coll.find_one(query, {'_id': 1}) is not None


def claim(owner: str, lease_seconds: float,
          now: Optional[datetime.datetime] = None) -> Optional[Job]:
    """ Start the oldest job that is ready and not held back, leasing it to
    `owner` for `lease_seconds`. Returns None if there is no such job """
    now = now or utcnow()
    coll = Job._get_collection()
    ready = {'status': 'queued', '$or': [{'not_before': None},
                                         {'not_before': {'$lte': now}}]}
    candidates = coll.find(ready).sort([('created', 1), ('_id', 1)]) \
                     .limit(CLAIM_CANDIDATES)
    for doc in candidates:
        if _blocked(doc):
            continue
        fields = {'status': 'running', 'started': now,
                  'lease': {'owner': owner,
                            'expires': now + datetime.timedelta(
                                seconds=lease_seconds)}}
        if doc.get('serial'):
            fields['slot'] = f"edit:{doc.get('scope') or doc['_id']}"
        try:
            got = coll.find_one_and_update(
                {'_id': doc['_id'], 'status': 'queued'},
                {'$set': fields, '$unset': {'not_before': ''},
                 '$inc': {'attempts': 1}},
                return_document=ReturnDocument.AFTER)
        except DuplicateKeyError:
            continue   # another serial job in this scope is running
        if got is not None:
            return Job._from_son(got)
    return None


def renew(owner: str, job_ids, lease_seconds: float) -> List[str]:
    """ Extend the leases `owner` holds on `job_ids`. Returns the ids that
    are still held; the others were reaped or finished """
    expires = utcnow() + datetime.timedelta(seconds=lease_seconds)
    coll = Job._get_collection()
    held = []
    for job_id in job_ids:
        oid = _oid(job_id)
        if oid is not None and coll.update_one(
                {'_id': oid, 'status': 'running', 'lease.owner': owner},
                {'$set': {'lease.expires': expires}}).matched_count:
            held.append(str(job_id))
    return held


def _finish_fields(now, keep_days) -> dict:
    return {'finished': now,
            'expires_at': now + datetime.timedelta(days=keep_days)}


def reap(now: Optional[datetime.datetime] = None, keep_days: float = 7
         ) -> List[str]:
    """ Deal with running jobs whose lease ran out, which means their
    process died: idempotent jobs with attempts left are queued again, the
    others are abandoned. Returns the ids handled """
    now = now or utcnow()
    coll = Job._get_collection()
    handled = []
    for doc in list(coll.find({'status': 'running',
                               'lease.expires': {'$lte': now}})):
        try:
            spec = get_kind(doc['kind'])
            again = (spec.idempotent
                     and doc.get('attempts', 0) < doc.get('max_attempts', 3))
        except KeyError:
            again = False
        if again:
            to, fields = 'queued', {}
        else:
            to = 'abandoned'
            fields = dict(_finish_fields(now, keep_days),
                          error='The process running this job stopped '
                                'responding')
        # the lease condition makes sure only one reaper handles the job
        if _reap_one(doc['_id'], to, fields, now):
            handled.append(str(doc['_id']))
    return handled


def _reap_one(oid, to, fields, now) -> bool:
    query = {'_id': oid, 'status': 'running', 'lease.expires': {'$lte': now}}
    update = {'$set': dict(fields, status=to),
              '$unset': {'lease': '', 'slot': ''}}
    return Job._get_collection().update_one(query, update) \
        .modified_count == 1


def finish(job_id, owner: str, status: str, result: Optional[dict] = None,
           error: Optional[str] = None, traceback: Optional[str] = None,
           error_type: Optional[str] = None, keep_days: float = 7) -> bool:
    """ Record the end of a job leased to `owner`. False if it isn't """
    if status not in FINISHED:
        raise ValueError(f"{status!r} isn't a final status")
    fields = _finish_fields(utcnow(), keep_days)
    for key, value in (('result', result), ('error', error),
                       ('traceback', traceback), ('error_type', error_type)):
        if value is not None:
            fields[key] = value
    return transition(job_id, status, owner=owner, set=fields,
                      unset=('lease', 'slot'))


def requeue(job_id, owner: str, delay: float = 0,
            message: Optional[str] = None) -> bool:
    """ Put a running job back in the queue to be claimed after `delay`
    seconds, without counting the attempt it used """
    fields = {'not_before': utcnow() + datetime.timedelta(seconds=delay)}
    if message is not None:
        fields['progress.message'] = message
    return transition(job_id, 'queued', owner=owner, set=fields,
                      unset=('lease', 'slot'), inc={'attempts': -1})


def request_cancel(job_id) -> Optional[str]:
    """ Cancel a queued job, or ask a running one to stop at its next
    check. Returns 'cancelled', 'requested', 'finished' if it had already
    ended, or None if there is no such job """
    oid = _oid(job_id)
    if oid is None:
        return None
    coll = Job._get_collection()
    for _ in range(3):   # the job may move between looking and acting
        doc = coll.find_one({'_id': oid}, {'status': 1})
        if doc is None:
            return None
        if doc['status'] == 'queued':
            if transition(oid, 'cancelled', expect='queued',
                          set=_finish_fields(utcnow(), 7)):
                return 'cancelled'
        elif doc['status'] == 'running':
            if coll.update_one({'_id': oid, 'status': 'running'},
                               {'$set': {'cancel_requested': True}}
                               ).matched_count:
                return 'requested'
        else:
            return 'finished'
    return 'finished'


def serial_running(scope: str, exclude=None) -> bool:
    """ Whether a serial job other than `exclude` is running in `scope` """
    query = {'scope': scope, 'serial': True, 'status': 'running'}
    oid = _oid(exclude) if exclude is not None else None
    if oid is not None:
        query['_id'] = {'$ne': oid}
    return Job._get_collection().find_one(query, {'_id': 1}) is not None


def active_for_scope(scope: str, exclude=None) -> List[dict]:
    """ The queued and running jobs of `scope`, oldest first, as dicts for
    display. Uses the (scope, status) index """
    query = {'scope': scope, 'status': {'$in': list(ACTIVE)}}
    oid = _oid(exclude) if exclude is not None else None
    if oid is not None:
        query['_id'] = {'$ne': oid}
    found = []
    for doc in Job._get_collection().find(query).sort(
            [('created', 1), ('_id', 1)]):
        progress = doc.get('progress') or {}
        total = progress.get('total')
        try:
            label = get_kind(doc['kind']).label or doc['kind']
        except KeyError:
            label = doc['kind']
        found.append({
            'id': str(doc['_id']), 'kind': doc['kind'], 'label': label,
            'status': doc['status'], 'user': doc.get('user'),
            'created': doc['created'],
            'stage': progress.get('stage'),
            'message': progress.get('message'),
            'percent': (int(round(100 * progress.get('done', 0) / total))
                        if total else None)})
    return found
