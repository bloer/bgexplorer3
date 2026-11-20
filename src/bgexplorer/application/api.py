""" JSON API. Versions are addressed by name, e.g.
GET /api/v1/versions/<version_tag>

Errors are returned as {"error": {"message": str, "fields": {name: str}}},
where "fields" is only present for validation errors of particular fields.
Requests with a body must be sent as application/json, except uploads of
version files to POST /api/v1/versions/import, which are multipart forms.
"""
import datetime
import re
import flask
from bson import ObjectId
from mongoengine.base.document import NON_FIELD_ERRORS
from mongoengine.errors import ValidationError, FieldDoesNotExist
from pint.errors import PintError
from werkzeug.exceptions import HTTPException
from ..models.settings import VersionSettings, HitEffDbConfig, get_settings
from ..models.hiteff import require_no_spectra
from ..versioning.record import VersionRecord, get_record
from ..versioning import control as vc
from ..versioning.control import ProtectedVersionError
from ..versioning.verdoc import ReadOnlyVersionError, VersionedDocument
from ..jobs import maintenance as job_maintenance, models as job_models
from . import jobs as appjobs
from .auth import require, has_role, Role, SAFE_METHODS

API_VERSION = 'v1'

# endpoints that take multipart file uploads, or nothing, instead of JSON
UPLOAD_ENDPOINTS = ('api.import_version', 'api.cancel_job')
# how long a request may be asked to wait for its job, at the most
MAX_WAIT_SECONDS = 3600
# what to tell clients that were sent a job to poll, in seconds
RETRY_AFTER_SECONDS = 2

# Fields of a version that can be changed with PATCH: the description is
# the VersionRecord's, the others are VersionSettings'
SETTINGS_FIELDS = ('description', 'addsources', 'hiteffdbconfig')
HITEFFDBCONFIG_FIELDS = ('rois', 'display_scalars', 'display_spectra',
                         'extra_columns')
# fields that are never returned
HIDDEN_FIELDS = ('_id', '_cls', 'cache_token', 'calc_token', 'bin_edges')
# the VersionSettings fields returned with a version
SETTINGS_RESULT_FIELDS = ('addsources', 'hiteffdbconfig')


class APIError(Exception):
    """ Error returned to the client as JSON """
    def __init__(self, message: str, status: int = 400,
                 fields: dict = None, problems: list = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.fields = fields
        self.problems = problems


def jsonable(value):
    """ Convert the output of `to_mongo` to plain JSON types """
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()
                if k not in HIDDEN_FIELDS}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    return value


def version_type(record: VersionRecord) -> str:
    return 'branch' if record.editable else 'tag'


def version_to_json(record: VersionRecord) -> dict:
    """ Short description of a version for listing """
    return dict(version_tag=record.version_tag,
                description=record.description,
                editable=record.editable,
                type=version_type(record),
                modified=jsonable(record.modified),
                url=flask.url_for('api.get_version',
                                  active_version=record.version_tag))


def settings_to_json(record: VersionRecord) -> dict:
    """ A version with its settings """
    son = get_settings(record.version_tag, create=False).to_mongo().to_dict()
    result = jsonable({k: v for k, v in son.items()
                       if k in SETTINGS_RESULT_FIELDS})
    result.update(version_to_json(record))
    result.setdefault('description', None)
    return result


def validation_fields(error, prefix: str = '', named: bool = False
                      ) -> dict:
    """ Flatten a mongoengine ValidationError into {dotted field: message}.
    List indices and map keys are path elements. If `named`, a leaf error's
    `field_name` is appended to `prefix`, as for errors raised by `clean`
    """
    errors = error.errors if isinstance(error, ValidationError) else error
    if isinstance(errors, dict) and errors:
        fields = {}
        for name, sub in errors.items():
            if name == NON_FIELD_ERRORS:
                # raised by clean, which may name the field
                fields.update(validation_fields(sub, prefix, named=True))
            else:
                fields.update(validation_fields(sub, f"{prefix}{name}."))
        return fields
    name = getattr(error, 'field_name', None) if named else None
    message = error.message if isinstance(error, ValidationError) \
        else str(error)
    return {f"{prefix}{name}" if name else prefix.rstrip('.') or 'settings':
            message}


def parse_field(doc_cls, name: str, value, prefix: str = ''):
    """ Convert a JSON value to the python value for field `name` """
    try:
        return doc_cls._fields[name].to_python(value)
    except ValidationError as e:
        raise APIError(f"Invalid value for {prefix}{name}",
                       fields=validation_fields(e, f"{prefix}{name}.",
                                                named=True)) from e
    except (FieldDoesNotExist, ValueError, TypeError, AttributeError,
            PintError) as e:
        raise APIError(f"Invalid value for {prefix}{name}",
                       fields={f"{prefix}{name}": str(e)}) from e


def get_json_object() -> dict:
    body = flask.request.get_json(silent=True)
    if not isinstance(body, dict):
        raise APIError("Request body must be a JSON object")
    return body


def get_version_record(version_tag: str) -> VersionRecord:
    try:
        return get_record(version_tag)
    except KeyError:
        raise APIError(f"Version '{version_tag}' not found", 404) from None


def parse_display_spectra(settings, value) -> dict:
    """ The new `display_spectra` for `settings`. The bins that spectra must
    have are set by the first spectrum, so they can't be given, but are kept.
    Removing a spectrum from the map resets its bins, which is only allowed
    when no hit efficiency has that spectrum
    """
    prefix = 'hiteffdbconfig.display_spectra'
    if isinstance(value, dict):
        for key, entry in value.items():
            if isinstance(entry, dict) and 'bin_edges' in entry:
                raise APIError(
                    "The bins of a spectrum can't be set",
                    fields={f"{prefix}.{key}.bin_edges":
                            "set by the first spectrum with this name"})
    new = parse_field(HitEffDbConfig, 'display_spectra', value,
                      'hiteffdbconfig.')
    for key, cfg in settings.hiteffdbconfig.display_spectra.items():
        if cfg.bin_edges is None:
            continue
        if new.get(key) is not None:
            new[key].bin_edges = cfg.bin_edges
            continue
        try:
            require_no_spectra(settings, key)
        except ValidationError as e:
            raise APIError(e.message, 409,
                           fields={f"{prefix}.{key}": e.message}) from e
    return new


def update_settings(record: VersionRecord, body: dict) -> None:
    """ Apply the changes in `body` to the version and save """
    unknown = set(body) - set(SETTINGS_FIELDS)
    if unknown:
        raise APIError("These fields can't be changed: "
                       f"{', '.join(sorted(unknown))}",
                       fields={name: "can't be changed" for name in unknown})
    settings = None
    if 'addsources' in body or 'hiteffdbconfig' in body:
        settings = get_settings(record.version_tag, create=False)
    if 'description' in body:
        record.description = parse_field(VersionRecord, 'description',
                                         body['description'])
    if 'addsources' in body:
        settings.addsources = parse_field(VersionSettings, 'addsources',
                                          body['addsources'])
    if 'hiteffdbconfig' in body:
        config = body['hiteffdbconfig']
        if not isinstance(config, dict):
            raise APIError("hiteffdbconfig must be an object",
                           fields={'hiteffdbconfig': "must be an object"})
        unknown = set(config) - set(HITEFFDBCONFIG_FIELDS)
        if unknown:
            raise APIError("Unknown hiteffdbconfig fields: "
                           f"{', '.join(sorted(unknown))}",
                           fields={f"hiteffdbconfig.{name}": "unknown field"
                                   for name in unknown})
        for name, value in config.items():
            if name == 'display_spectra':
                parsed = parse_display_spectra(settings, value)
            else:
                parsed = parse_field(HitEffDbConfig, name, value,
                                     'hiteffdbconfig.')
            setattr(settings.hiteffdbconfig, name, parsed)
    try:
        if settings is not None:
            settings.save()
        if 'description' in body:
            record.save()
    except ValidationError as e:
        raise APIError(f"Invalid settings: {e.message}",
                       fields=validation_fields(e)) from e
    # post_save touches the version in the db
    record.reload()


def check_editable(version_tag: str) -> None:
    """ Raise a PermissionError if the version is a tag """
    if not get_record(version_tag).editable:
        raise ReadOnlyVersionError(f"'{version_tag}' is a tag, which can't "
                                   "be changed")


def check_deletable(version_tag: str) -> None:
    """ Raise a PermissionError if the version can't be deleted from the web
    interface and API, i.e. it is the default version or a tag """
    if version_tag == VersionedDocument.get_default_tag():
        raise ProtectedVersionError(f"The default version '{version_tag}' "
                                    "can't be deleted")
    if not get_record(version_tag).editable:
        raise ProtectedVersionError(f"'{version_tag}' is a tag; tags can't "
                                    "be deleted")


def prepare_import(form, files) -> dict:
    """ Check the form that uploads the version file `file` to be imported as
    the version named `version_tag`, of `type` 'branch' or 'tag'. Keeps the
    upload in the folder for job files and returns the parameters of the job
    that imports it, which removes it. Raises APIError """
    tag = form.get('version_tag')
    tag = tag.strip() if isinstance(tag, str) else tag
    type_ = form.get('type') or 'branch'
    # the name and description are checked like those of a new branch
    tag, _, _, description = validate_new_version(
        tag, None, 'branch', form.get('description'))
    if type_ not in ('branch', 'tag'):
        raise APIError("type must be 'branch' or 'tag'",
                       fields={'type': "must be 'branch' or 'tag'"})
    upload = files.get('file')
    if upload is None or not upload.filename:
        raise APIError("Choose a version file to import",
                       fields={'file': "required"})
    if type_ == 'tag':
        require(Role.admin)
    path = appjobs.new_upload_path()
    upload.save(path)
    return dict(path=path, version_tag=tag, editable=type_ == 'branch',
                description=description)


def job_error(job) -> APIError:
    """ The error to reply with when `job` ended without succeeding """
    data = job.result or {}
    if job.status == 'failed' and 'status' in data:
        return APIError(job.error or "The job failed", data['status'],
                        data.get('fields'), data.get('problems'))
    return APIError(appjobs.failure_text(job),
                    409 if job.status == 'cancelled' else 500)


def wait_preference(header) -> tuple:
    """ What the `Prefer` request header asks for: (seconds to wait for a job
    or None for the default, the preference applied, if any). Preferences
    that can't be read are ignored """
    asked = None
    applied = None
    for token in (header or '').split(','):
        name, _, value = token.strip().partition('=')
        name = name.strip().lower()
        value = value.strip().strip('"')
        if name == 'respond-async' and asked is None:
            asked, applied = 0.0, 'respond-async'
        elif name == 'wait' and re.fullmatch(r'\d+', value):
            asked = min(float(value), MAX_WAIT_SECONDS)
            applied = f'wait={int(value)}'
    return asked, applied


def accepted(job) -> flask.Response:
    """ The reply that sends the client to follow `job` """
    response = flask.jsonify(appjobs.job_json(job))
    response.status_code = 202
    response.headers['Location'] = flask.url_for('api.get_job',
                                                 job_id=str(job.id))
    response.headers['Retry-After'] = str(RETRY_AFTER_SECONDS)
    return response


def submit_for_api(kind: str, params: dict, scope):
    """ Submit a job for an API request and wait for it as long as the client
    prefers, by default JOBS_API_WAIT_SECONDS. Returns (job, None) once it has
    succeeded, and (job, response) with the 202 response if it is still going.
    Raises the APIError that a failure is to be answered with """
    wait, applied = wait_preference(flask.request.headers.get('Prefer'))
    if wait is None:
        wait = flask.current_app.config['JOBS_API_WAIT_SECONDS']
    flask.g.preference_applied = applied
    job = appjobs.submit_and_wait(kind, params, scope, wait)
    if job.status == 'succeeded':
        return job, None
    if job.status in job_models.FINISHED:
        raise job_error(job)
    return job, accepted(job)


def merge_args(args) -> tuple:
    """ The (source, rule) of a merge request, or raise APIError """
    source = args.get('source')
    if not isinstance(source, str) or not vc.version_exists(source):
        raise APIError(f"Version '{source}' not found",
                       fields={'source': "not found"})
    if source == flask.g.active_version:
        raise APIError("Can't merge a version into itself",
                       fields={'source': "must be another version"})
    try:
        rule = vc.MergeRule(args.get('rule', 'newest'))
    except ValueError as e:
        raise APIError(str(e), fields={'rule': "must be one of " + ', '.join(
            r.value for r in vc.MergeRule)}) from e
    return source, rule


def plan_to_json(plan) -> dict:
    """ A MergePlan, with each class's items by original_id """
    def items(rows):
        return [dict(id=str(row.original_id), name=row.name) for row in rows]
    return dict(source=plan.source, target=plan.target,
                rule=plan.rule.value, fingerprint=plan.fingerprint,
                problems=plan.problems, changes=plan.changes,
                warnings=plan.warnings,
                classes={c.name: dict(add=items(c.add),
                                      replace=items(c.replace),
                                      keep=items(c.keep),
                                      target_only=items(c.target_only))
                         for c in plan.classes})


def validate_new_version(tag, fromtag=None, type_='branch',
                         description=None):
    """ Check the arguments to create a new version. Returns
    (tag, fromtag, type_, description) or raises APIError
    """
    fromtag = fromtag or None
    description = description or None
    if type_ not in ('branch', 'tag'):
        raise APIError("type must be 'branch' or 'tag'",
                       fields={'type': "must be 'branch' or 'tag'"})
    if type_ == 'tag' and not fromtag:
        raise APIError("A tag must be created from another version",
                       fields={'from': "required for a tag"})
    if description is not None and not isinstance(description, str):
        raise APIError("description must be a string",
                       fields={'description': "must be a string"})
    try:
        vc.validate_version_name(tag)
    except ValueError as e:
        raise APIError(str(e), fields={'version_tag': str(e)}) from e
    if vc.version_exists(tag):
        raise APIError(f"Version '{tag}' already exists", 409,
                       fields={'version_tag': "already exists"})
    if fromtag is not None and not vc.version_exists(fromtag):
        raise APIError(f"Version '{fromtag}' not found",
                       fields={'from': "not found"})
    return tag, fromtag, type_, description


def create_api() -> flask.Blueprint:
    api = flask.Blueprint('api', __name__)

    @api.before_request
    def check_role():
        # reading needs only the viewer role, checked for the whole app
        if flask.request.method not in SAFE_METHODS:
            return require(Role.editor)
        return None

    # The API is exempt from CSRF checks (see create_app). That is safe
    # because changes need a JSON body: browsers can't send one, or a
    # DELETE, cross-origin without a CORS preflight, which we never allow.
    # The SameSite=Lax session cookie also isn't sent on cross-site POSTs
    @api.before_request
    def require_json():
        # files are uploaded as multipart forms, which browsers can send
        # cross-origin, so those endpoints rely on the SameSite cookie
        if (flask.request.method in ('POST', 'PUT', 'PATCH')
                and flask.request.endpoint not in UPLOAD_ENDPOINTS
                and not flask.request.is_json):
            raise APIError("Content-Type must be application/json", 415)

    @api.after_request
    def say_what_was_preferred(response):
        applied = flask.g.get('preference_applied')
        if applied and 'Preference-Applied' not in response.headers:
            response.headers['Preference-Applied'] = applied
        return response

    @api.errorhandler(APIError)
    def api_error(e):
        error = dict(message=e.message)
        if e.fields:
            error['fields'] = e.fields
        if e.problems:
            error['problems'] = e.problems
        return flask.jsonify(error=error), e.status

    @api.errorhandler(PermissionError)
    def permission_error(e):
        return flask.jsonify(error=dict(message=str(e))), 403

    @api.errorhandler(HTTPException)
    def http_error(e):
        return flask.jsonify(error=dict(message=e.description)), e.code

    @api.get('/versions')
    def list_versions():
        return flask.jsonify(versions=[version_to_json(v)
                                       for v in vc.list_versions()])

    @api.post('/versions')
    def create_version():
        body = get_json_object()
        allowed = {'version_tag', 'description', 'from', 'type'}
        if unknown := set(body) - allowed:
            raise APIError(f"Unknown fields: {', '.join(sorted(unknown))}",
                           fields={name: "unknown field" for name in unknown})
        tag, fromtag, type_, description = validate_new_version(
            body.get('version_tag'), body.get('from'),
            body.get('type', 'branch'), body.get('description'))
        if type_ == 'tag':
            require(Role.admin)
        record = vc.create_version(tag, fromtag, editable=type_ == 'branch',
                                     description=description)
        response = flask.jsonify(settings_to_json(record))
        response.status_code = 201
        response.headers['Location'] = flask.url_for(
            'api.get_version', active_version=tag)
        return response

    def version_json(record):
        data = settings_to_json(record)
        data['pending_jobs'] = [
            {key: job[key] for key in ('id', 'kind', 'status', 'percent')}
            for job in appjobs.active_edit_jobs(record.version_tag)]
        return flask.jsonify(data)

    @api.get('/versions/<active_version>')
    def get_version():
        return version_json(get_version_record(flask.g.active_version))

    @api.patch('/versions/<active_version>')
    def update_version():
        tag = flask.g.active_version
        get_version_record(tag)
        check_editable(tag)
        job, early = submit_for_api(
            'update_version', {'version': tag, 'body': get_json_object()}, tag)
        return early or version_json(get_version_record(tag))

    @api.get('/versions/<active_version>/merge')
    def plan_merge():
        source, rule = merge_args(flask.request.args)
        return flask.jsonify(plan_to_json(vc.plan_merge(
            source, flask.g.active_version, rule)))

    @api.post('/versions/<active_version>/merge')
    def merge_version():
        body = get_json_object()
        allowed = {'source', 'rule', 'fingerprint'}
        if unknown := set(body) - allowed:
            raise APIError(f"Unknown fields: {', '.join(sorted(unknown))}",
                           fields={name: "unknown field" for name in unknown})
        source, rule = merge_args(body)
        fingerprint = body.get('fingerprint')
        if fingerprint is not None and not isinstance(fingerprint, str):
            raise APIError("fingerprint must be a string",
                           fields={'fingerprint': "must be a string"})
        job, early = submit_for_api('merge', {
            'version': flask.g.active_version, 'source': source,
            'rule': rule.value, 'fingerprint': fingerprint},
            flask.g.active_version)
        return early or flask.jsonify(job.result['plan'])

    @api.get('/versions/<active_version>/export')
    def export_version():
        tag = flask.g.active_version
        get_version_record(tag)
        job, early = submit_for_api('export_version', {'version': tag}, None)
        return early or appjobs.send_export(job)

    @api.post('/versions/import')
    def import_version():
        params = prepare_import(flask.request.form, flask.request.files)
        tag = params['version_tag']
        job, early = submit_for_api('import_version', params, tag)
        if early:
            return early
        response = flask.jsonify(settings_to_json(get_record(tag)))
        response.status_code = 201
        response.headers['Location'] = flask.url_for(
            'api.get_version', active_version=tag)
        return response

    @api.delete('/versions/<active_version>')
    def delete_version():
        tag = flask.g.active_version
        get_version_record(tag)
        check_deletable(tag)
        job, early = submit_for_api('delete_version', {'version': tag}, tag)
        return early or ('', 204)

    @api.get('/jobs')
    def list_jobs():
        args = flask.request.args
        status = args.get('status')
        if status is not None and status not in job_models.STATUSES:
            raise APIError(f"Unknown status '{status}'",
                           fields={'status': "must be one of "
                                   + ', '.join(job_models.STATUSES)})
        try:
            limit = int(args.get('limit', 100))
            if limit < 1:
                raise ValueError
        except ValueError:
            raise APIError("limit must be a positive integer",
                           fields={'limit': "must be a positive integer"}
                           ) from None
        jobs = job_maintenance.find_jobs(
            status=status, kind=args.get('kind'), scope=args.get('version'),
            user=None if has_role(Role.site_admin)
            else appjobs.current_user_name(), limit=min(limit, 500))
        return flask.jsonify(jobs=[appjobs.job_json(j) for j in jobs])

    @api.get('/jobs/<job_id>')
    def get_job(job_id):
        return flask.jsonify(appjobs.job_json(appjobs.visible_job(job_id)))

    @api.post('/jobs/<job_id>/cancel')
    def cancel_job(job_id):
        job = appjobs.visible_job(job_id)
        outcome = job_models.request_cancel(job.id)
        if outcome not in ('cancelled', 'requested'):
            raise APIError(f"The job has already {job.status}", 409)
        response = flask.jsonify(appjobs.job_json(job_models.get(job.id)))
        response.status_code = 200 if outcome == 'cancelled' else 202
        return response

    return api
