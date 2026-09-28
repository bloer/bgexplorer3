""" JSON API. Versions are addressed by name, e.g.
GET /api/v1/versions/<version_tag>

Errors are returned as {"error": {"message": str, "fields": {name: str}}},
where "fields" is only present for validation errors of particular fields.
Requests with a body must be sent as application/json.
"""
import datetime
import flask
from bson import ObjectId
from mongoengine.base.document import NON_FIELD_ERRORS
from mongoengine.errors import ValidationError, FieldDoesNotExist
from pint.errors import PintError
from werkzeug.exceptions import HTTPException
from ..models.settings import VersionSettings, HitEffDbConfig
from ..models import versioncontrol as vc

API_VERSION = 'v1'

# VersionSettings fields that can be changed with PATCH
SETTINGS_FIELDS = ('description', 'addsources', 'hiteffdbconfig')
HITEFFDBCONFIG_FIELDS = ('rois', 'display_scalars', 'display_spectra',
                         'extra_columns')
# fields that are never returned
HIDDEN_FIELDS = ('_id', '_cls', 'cache_token')


class APIError(Exception):
    """ Error returned to the client as JSON """
    def __init__(self, message: str, status: int = 400,
                 fields: dict = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.fields = fields


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


def version_type(settings: VersionSettings) -> str:
    return 'branch' if settings.editable else 'tag'


def version_to_json(settings: VersionSettings) -> dict:
    """ Short description of a version for listing """
    return dict(version_tag=settings.version_tag,
                description=settings.description,
                editable=settings.editable,
                type=version_type(settings),
                modified=jsonable(settings.modified),
                url=flask.url_for('api.get_version',
                                  active_version=settings.version_tag))


def settings_to_json(settings: VersionSettings) -> dict:
    """ Full VersionSettings """
    result = jsonable(settings.to_mongo().to_dict())
    result.update(version_to_json(settings))
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


def get_version_settings(version_tag: str) -> VersionSettings:
    try:
        return VersionSettings.objects.get(version_tag=version_tag)
    except VersionSettings.DoesNotExist:
        raise APIError(f"Version '{version_tag}' not found", 404) from None


def update_settings(settings: VersionSettings, body: dict) -> None:
    """ Apply the changes in `body` to `settings` and save """
    unknown = set(body) - set(SETTINGS_FIELDS)
    if unknown:
        raise APIError("These fields can't be changed: "
                       f"{', '.join(sorted(unknown))}",
                       fields={name: "can't be changed" for name in unknown})
    for name in ('description', 'addsources'):
        if name in body:
            setattr(settings, name,
                    parse_field(VersionSettings, name, body[name]))
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
            setattr(settings.hiteffdbconfig, name,
                    parse_field(HitEffDbConfig, name, value,
                                'hiteffdbconfig.'))
    try:
        settings.save()
    except ValidationError as e:
        raise APIError(f"Invalid settings: {e.message}",
                       fields=validation_fields(e)) from e
    # post_save touches the settings in the db
    settings.reload()


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
    def require_json():
        if (flask.request.method in ('POST', 'PUT', 'PATCH')
                and not flask.request.is_json):
            raise APIError("Content-Type must be application/json", 415)

    @api.errorhandler(APIError)
    def api_error(e):
        error = dict(message=e.message)
        if e.fields:
            error['fields'] = e.fields
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
        settings = vc.create_version(tag, fromtag, editable=type_ == 'branch',
                                     description=description)
        response = flask.jsonify(settings_to_json(settings))
        response.status_code = 201
        response.headers['Location'] = flask.url_for(
            'api.get_version', active_version=tag)
        return response

    @api.get('/versions/<active_version>')
    def get_version():
        return flask.jsonify(settings_to_json(
            get_version_settings(flask.g.active_version)))

    @api.patch('/versions/<active_version>')
    def update_version():
        settings = get_version_settings(flask.g.active_version)
        update_settings(settings, get_json_object())
        return flask.jsonify(settings_to_json(settings))

    @api.delete('/versions/<active_version>')
    def delete_version():
        get_version_settings(flask.g.active_version)
        vc.delete_version(flask.g.active_version)
        return '', 204

    return api
