""" The kinds of background job the application runs. Importing this module
registers them.

The jobs that edit a version are serial: one at a time per version, in the
order they were submitted. They wait, rather than fail, while another
operation such as a merge has the version locked.
"""
from bson import ObjectId
from bson.errors import InvalidId
from mongoengine import ValidationError
from werkzeug.datastructures import MultiDict
from .. import jobs
from ..models.component import Component, LocationMode
from ..models.settings import get_settings
from ..versioning.record import get_record
from ..versioning.verdoc import check_writable, VersionLockedError
from .blueprints import CollectionViews, REFERENCED_CLASSES, subclass_named
from .forms import update_object, list_snapshot, edited_rows
from .jobs import build_url

EDIT = dict(serial=True, retry_on=(VersionLockedError,))

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
