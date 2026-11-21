""" The kinds of background job the application runs. Importing this module
registers them.

The jobs that edit a version are serial: one at a time per version, in the
order they were submitted. They wait, rather than fail, while another
operation such as a merge has the version locked.
"""
import functools
import os
from bson import ObjectId
from bson.errors import InvalidId
from mongoengine import ValidationError
from werkzeug.datastructures import MultiDict
from .. import jobs
from ..models import signals
from ..models.component import Component, LocationMode
from ..models.hiteff import reset_binning
from ..models.settings import get_settings
from ..versioning import archive
from ..versioning import control as vc
from ..versioning.record import get_record
from ..versioning.verdoc import check_writable, VersionLockedError
from .api import APIError, update_settings, plan_to_json
from .blueprints import CollectionViews, REFERENCED_CLASSES, subclass_named
from .forms import update_object, list_snapshot, edited_rows
from .jobs import build_url, export_path

EDIT = dict(serial=True, retry_on=(VersionLockedError,))
# operations on a whole version wait for everything else to end, and
# everything else for them
WHOLE = dict(serial=True, exclusive=True, retry_on=(VersionLockedError,))


class Reported(Exception):
    """ A failure that the person who asked for the job should be told about
    as it is, with an HTTP `status`, the `fields` of a form to blame and
    `problems` to list. All of it is kept in the failed job's result """
    def __init__(self, message: str, status: int = 400, fields: dict = None,
                 problems: list = None):
        super().__init__(message)
        self.job_data = {'status': status}
        if fields:
            self.job_data['fields'] = fields
        if problems:
            self.job_data['problems'] = list(problems)


def reported(func):
    """ Make a kind raise `Reported` for the failures that are a reply to the
    request rather than a fault. A locked version is passed on so that the job
    waits for it """
    @functools.wraps(func)
    def wrapper(params):
        try:
            return func(params)
        except (Reported, VersionLockedError):
            raise
        except APIError as e:
            raise Reported(e.message, e.status, e.fields, e.problems) from e
        except archive.VersionFileError as e:
            raise Reported(str(e), 400, {'file': str(e)}, e.problems) from e
        except vc.MergeError as e:
            raise Reported(str(e), 409, problems=e.problems) from e
        except PermissionError as e:
            raise Reported(str(e), 403) from e
        except KeyError as e:
            raise Reported(str(e.args[0]) if e.args else "Not found",
                           404) from e
        except ValueError as e:
            raise Reported(str(e), 400) from e
    return wrapper


DOCUMENT_CLASSES = {cls.__name__: cls for cls, _ in REFERENCED_CLASSES}


def _load(cls, version: str, objid: str, whole: bool = False):
    """ The document `objid` of `cls` in `version`. Like a request that
    doesn't need them, leave out the large fields unless `whole` """
    queryset = cls.select_version(version)
    if not whole:
        skip = [f for f in CollectionViews.DEFERRED_FIELDS
                if f.split('__')[0] in cls._fields]
        if skip:
            queryset = queryset.exclude(*skip)
    try:
        return queryset.get(original_id=ObjectId(objid))
    except (cls.DoesNotExist, InvalidId):
        raise LookupError(f"No {cls.__name__} with id {objid} in "
                          f"'{version}'; it may have been deleted") from None


def _view_url(cls, version: str, obj) -> str:
    return build_url(f'{cls.__name__.lower()}.view',
                     objid=str(obj.original_id), active_version=version)


@jobs.job_kind('save_document', label='Saving a document', **EDIT)
def save_document(params):
    """ Apply a submitted form to a document, or to a new one, and save it.
    `params`: cls, version, objid (None for a new document), type (of a new
    one), owner (the id of the component that a new spec belongs to),
    settings (what to attach it with) and form ({field: [values]}) """
    version = params['version']
    cls = DOCUMENT_CLASSES[params['cls']]
    check_writable(version)
    jobs.progress(0, stage='saving')
    owner = None
    if params['objid']:
        obj = _load(cls, version, params['objid'])
    else:
        klass = subclass_named(cls, params['type']) if params['type'] else cls
        obj = klass(version_tag=version)
        if params['owner']:
            owner = _load(Component, version, params['owner'])
            obj.owner = owner
    form = MultiDict(params['form'])
    # generated emission sources the user edits become overrides
    if overrides := hasattr(obj, 'mark_overrides'):
        shown = list_snapshot(obj, 'sources', obj.OVERRIDE_FIELDS)
    errors = {}
    obj = update_object(obj, form, errors=errors)
    if overrides:
        obj.mark_overrides(edited_rows(shown, form, 'sources'))
    if errors:
        raise ValidationError("The form does not fit the document any more: "
                              + '; '.join(f'{k}: {v}'
                                          for k, v in errors.items()))
    if owner is not None:
        settings = dict(params['settings'])
        if 'location_mode' in settings:
            settings['location_mode'] = LocationMode(settings['location_mode'])
        owner.add_owned_spec(obj, **settings)
        target = owner
        view = Component
    else:
        obj.save()
        target = obj
        view = cls
    return {'messages': [[f"Successfully saved {obj}", 'success']],
            'redirect': _view_url(view, version, target)}


@jobs.job_kind('delete_document', label='Deleting a document', **EDIT)
def delete_document(params):
    """ `params`: cls, version, objid """
    version = params['version']
    cls = DOCUMENT_CLASSES[params['cls']]
    check_writable(version)
    obj = _load(cls, version, params['objid'])
    # only removes it from this version
    obj.delete()
    return {'messages': [[f"Deleted {obj} from {version}", 'success']],
            'redirect': build_url(f'{cls.__name__.lower()}.overview',
                                  active_version=version)}


@jobs.job_kind('clone_document', label='Copying a document', **EDIT)
def clone_document(params):
    """ `params`: cls, version, objid """
    version = params['version']
    cls = DOCUMENT_CLASSES[params['cls']]
    check_writable(version)
    original = _load(cls, version, params['objid'], whole=True)
    copy = original.clone()
    if hasattr(copy, 'name'):
        copy.name = f"{original.name} (copy)"
    copy.save()
    return {'messages': [[f"Created {copy} as a copy of {original}",
                          'success']],
            'redirect': build_url(f'{cls.__name__.lower()}.edit',
                                  objid=str(copy.original_id),
                                  active_version=version)}


@jobs.job_kind('save_settings', label='Saving version settings', **EDIT)
def save_settings(params):
    """ `params`: version, form ({field: [values]}, already limited to the
    fields that can be edited). Saving evaluates the ROIs for all the hit
    efficiencies, which is what takes the time """
    tag = params['version']
    check_writable(tag)
    jobs.progress(0, stage='saving')
    form = MultiDict(params['form'])
    settings = get_settings(tag, create=False)
    record = get_record(tag)
    errors = {}
    update_object(settings, form, errors=errors)
    update_object(record, form, errors=errors)
    if errors:
        raise ValidationError("The form does not fit the settings any more: "
                              + '; '.join(f'{k}: {v}'
                                          for k, v in errors.items()))
    settings.save()
    record.save()
    return {'messages': [['Settings saved', 'success']],
            'redirect': build_url('overview', active_version=tag)}


@jobs.job_kind('update_version', label='Updating a version', **EDIT)
@reported
def update_version(params):
    """ `params`: version, body (the changes, as for PATCH of the API) """
    tag = params['version']
    check_writable(tag)
    update_settings(get_record(tag), params['body'])
    return {'version': tag}


@jobs.job_kind('reset_binning', label='Resetting spectrum binning', **EDIT)
@reported
def reset_spectrum_binning(params):
    """ `params`: version, key (of the spectrum) """
    tag, key = params['version'], params['key']
    check_writable(tag)
    try:
        reset_binning(get_settings(tag, create=False), key)
    except ValidationError as e:
        raise Reported(e.message, 409,
                       {f'hiteffdbconfig.display_spectra.{key}': e.message}
                       ) from e
    return {'messages': [[f"The next spectrum '{key}' sets its binning",
                          'success']],
            'redirect': build_url('edit_settings', active_version=tag)}


@jobs.job_kind('rebuild', label='Rebuilding source terms', **EDIT)
@reported
def rebuild(params):
    """ `params`: version """
    version = params['version']
    check_writable(version)
    count = signals.rebuild_sourceterms(version)
    return {'messages': [[f"Rebuilt {count} source terms in '{version}'",
                          'success']],
            'redirect': build_url('admin.maintenance_page')}


@jobs.job_kind('merge', label='Merging versions', **WHOLE)
@reported
def merge(params):
    """ `params`: version (the target), source, rule (its value) and
    fingerprint (of the plan that was shown, or None) """
    target = params['version']
    jobs.progress(0, stage='merging')
    plan = vc.merge_version(params['source'], target,
                            vc.MergeRule(params['rule']),
                            params['fingerprint'])
    source = params['source']
    text = (f"Merged '{source}' into '{target}': {vc._merge_summary(plan)}"
            if plan.changes else f"Nothing to merge from '{source}'")
    return {'plan': plan_to_json(plan), 'messages': [[text, 'success']],
            'redirect': build_url('overview', active_version=target)}


@jobs.job_kind('import_version', label='Importing a version file', **WHOLE)
@reported
def import_version_file(params):
    """ `params`: path (of the uploaded file, which is removed), version_tag,
    editable and description """
    tag = params['version_tag']
    jobs.progress(0, stage='importing')
    try:
        with open(params['path'], 'rb') as fileobj:
            try:
                archive.import_version(fileobj, tag,
                                       editable=params['editable'],
                                       description=params['description'])
            except KeyError as e:
                raise Reported(str(e.args[0]), 409,
                               {'version_tag': "already exists"}) from e
    finally:
        try:
            os.remove(params['path'])
        except FileNotFoundError:
            pass
    return {'version_tag': tag,
            'messages': [[f"Imported '{tag}'", 'success']],
            'redirect': build_url('overview', active_version=tag)}


@jobs.job_kind('delete_version', label='Deleting a version', **WHOLE)
@reported
def delete_version(params):
    """ `params`: version """
    tag = params['version']
    jobs.progress(0, stage='deleting')
    vc.delete_version(tag, allow_tags=False)
    return {'messages': [[f"Deleted version '{tag}'", 'success']],
            'redirect': build_url('index')}


@jobs.job_kind('export_version', idempotent=True,
               label='Exporting a version')
@reported
def export_version_file(params):
    """ `params`: version. Leaves the file in the folder for job files, where
    it can be downloaded from the job """
    tag = params['version']
    job_id = jobs.current_job_id()
    path = export_path(job_id)
    jobs.progress(0, stage='writing the file')
    try:
        with open(path, 'wb') as fileobj:
            archive.export_version(tag, fileobj)
    except BaseException:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        raise
    return {'filename': f"{tag}.bgx.tar.gz", 'size': os.path.getsize(path),
            'download': build_url('jobs.download', job_id=str(job_id))}
