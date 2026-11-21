""" HTML pages to create and delete versions and edit their settings """
import flask
from mongoengine.errors import ValidationError
from werkzeug.datastructures import MultiDict
from ..versioning import control as vc
from ..versioning.verdoc import check_writable
from ..jobs import models as job_models
from ..versioning import maintenance
from ..models.settings import (get_settings, VersionSettings,
                               SETTINGS_ORIGINAL_ID)
from ..versioning.record import get_record
from ..versioning.diff import diff_versions, compare_document
from ..models.hiteff import HitEfficiency
from ..models.histogram import describe_bins
from .api import (APIError, validate_new_version, validation_fields,
                  prepare_import, check_deletable, job_error)
from .forms import update_object, LISTFIELDS_KEY
from . import jobs as appjobs
from .blueprints import format_raw_value, form_params
from .auth import require, role_required, Role


def create_versions_blueprint() -> flask.Blueprint:
    bp = flask.Blueprint('versions', __name__)
    # every page here makes or deletes a version
    bp.before_request(lambda: require(Role.editor))

    @bp.route('/new', methods=['GET', 'POST'])
    def new():
        req = flask.request
        form = req.form if req.method == 'POST' else req.args
        errors = {}
        if req.method == 'POST':
            try:
                tag, fromtag, type_, description = validate_new_version(
                    form.get('version_tag', '').strip(), form.get('from'),
                    form.get('type', 'branch'), form.get('description'))
            except APIError as e:
                errors = e.fields or {'__all__': e.message}
            else:
                # tags can't be deleted from the web interface
                if (type_ == 'tag'
                        and (response := require(Role.admin)) is not None):
                    return response
                vc.create_version(tag, fromtag, editable=type_ == 'branch',
                                  description=description)
                kind = 'tag' if type_ == 'tag' else 'branch'
                source = f" from '{fromtag}'" if fromtag else ''
                flask.flash(f"Created {kind} '{tag}'{source}", 'success')
                return flask.redirect(flask.url_for('overview',
                                                    active_version=tag))
        return flask.render_template('versions_new.html', form=form,
                                     errors=errors,
                                     versions=vc.list_versions()), \
            400 if errors else 200

    @bp.route('/import', methods=['GET', 'POST'])
    def import_file():
        """ Create a new version from an exported version file """
        req = flask.request
        form = req.form if req.method == 'POST' else req.args
        errors, problems, status = {}, [], 200
        if req.method == 'POST':
            try:
                params = prepare_import(req.form, req.files)
                tag = params['version_tag']
                job = appjobs.submit_and_wait('import_version', params, tag)
                if job.status in job_models.FINISHED \
                        and job.status != 'succeeded':
                    raise job_error(job)
            except APIError as e:
                errors = dict(e.fields or {}, __all__=e.message)
                problems = e.problems or []
                status = e.status
            else:
                if job.status == 'succeeded':
                    appjobs.flash_result(job)
                    return flask.redirect(job.result['redirect'])
                return appjobs.waiting_page(job, flask.url_for(
                    'overview', active_version=tag))
        return flask.render_template('versions_import.html', form=form,
                                     errors=errors, problems=problems), \
            status

    @bp.route('/<active_version>/delete', methods=['GET', 'POST'])
    def delete():
        tag = flask.g.active_version
        record = get_record(tag)
        if flask.request.method == 'POST':
            # the model refuses to delete the default version and tags
            check_deletable(tag)
            return appjobs.submit_and_redirect(
                'delete_version', {'version': tag}, tag,
                flask.url_for('overview'))
        try:
            check_deletable(tag)
            protected = False
        except PermissionError:
            protected = True
        return flask.render_template('versions_delete.html',
                                     summary=vc.version_summary(tag),
                                     protected=protected,
                                     istag=not record.editable)

    @bp.route('/<active_version>/merge', methods=['GET', 'POST'])
    def merge():
        """ Preview merging another version into the active one, and do it
        on confirmation
        """
        tag = flask.g.active_version
        req = flask.request
        form = req.form if req.method == 'POST' else req.args
        source = form.get('source') or None
        try:
            rule = vc.MergeRule(form.get('rule') or 'newest')
        except ValueError:
            flask.abort(400, f"Unknown merge rule '{form.get('rule')}'")
        if source is not None and not vc.version_exists(source):
            flask.abort(404, f"Version '{source}' does not exist")
        plan, error, status = None, None, 200
        if source is not None and source != tag:
            # settings are made when first needed, which would change the
            # versions between planning and merging
            get_settings(source, create=False)
            get_settings(tag, create=False)
            if req.method == 'POST':
                job = appjobs.submit_and_wait('merge', {
                    'version': tag, 'source': source, 'rule': rule.value,
                    'fingerprint': form.get('fingerprint') or None}, tag)
                if job.status == 'succeeded':
                    appjobs.flash_result(job)
                    return flask.redirect(job.result['redirect'])
                if job.status in job_models.FINISHED:
                    error = job_error(job)
                    status = error.status
                else:
                    return appjobs.waiting_page(job, flask.url_for('overview'))
            plan = vc.plan_merge(source, tag, rule)
        elif source == tag:
            error, status = "Can't merge a version into itself", 400
        return flask.render_template('versions_merge.html', source=source,
                                     rule=rule, rules=list(vc.MergeRule),
                                     plan=plan, error=error), status

    @bp.post('/<active_version>/unlock')
    def unlock():
        """ Clear a lock left behind by an operation that didn't finish """
        if (response := require(Role.admin)) is not None:
            return response
        tag = flask.g.active_version
        if maintenance.clear_locks([tag]):
            flask.flash(f"Cleared the lock on '{tag}'", 'success')
        return flask.redirect(flask.url_for('overview'))

    @bp.post('/<active_version>/reset-binning')
    def reset_binning():
        """ Make the next spectrum with the form's `key` set the bins that
        spectra of that name must have, if there are no such spectra """
        if (response := require(Role.admin)) is not None:
            return response
        tag = flask.g.active_version
        check_writable(tag)
        key = flask.request.form.get('key')
        if not key:
            flask.abort(400, "Choose a spectrum")
        return appjobs.submit_and_redirect(
            'reset_binning', {'version': tag, 'key': key}, tag,
            flask.url_for('edit_settings', active_version=tag))

    return bp


# form fields the settings editor may change
SETTINGS_FORM_FIELDS = ('description', 'addsources', 'hiteffdbconfig')


def _settings_form(form) -> MultiDict:
    """ The entries of `form` for SETTINGS_FORM_FIELDS """
    def allowed(name):
        return any(name == f or name.startswith((f + '.', f + '['))
                   for f in SETTINGS_FORM_FIELDS)
    return MultiDict([(name, value) for name, value in form.items(multi=True)
                      if allowed(value if name == LISTFIELDS_KEY else name)])


@role_required(Role.editor)
def edit_settings():
    """ Edit the description and settings of the active version """
    tag = flask.g.active_version
    check_writable(tag)
    record = get_record(tag)
    settings = get_settings(tag, create=False)
    errors = {}
    if flask.request.method == 'POST':
        form = _settings_form(flask.request.form)
        update_object(settings, form, errors=errors)
        update_object(record, form, errors=errors)
        if not errors:
            # so that mistakes are shown with the form; the job saves them
            try:
                settings.validate()
                record.validate()
            except ValidationError as e:
                errors = validation_fields(e)
            else:
                return appjobs.submit_and_redirect(
                    'save_settings',
                    {'version': tag, 'form': form_params(form)},
                    tag, flask.url_for('overview'))
    # show display settings for every key in the hit efficiencies
    config = settings.hiteffdbconfig
    hiteffs = HitEfficiency.select_version(tag)
    keys = {type_: list(dict.fromkeys(
                list(getattr(config, f'display_{type_}'))
                + sorted(hiteffs.distinct(f'{type_}_keys'))))
            for type_ in ('scalars', 'spectra')}
    binnings = {key: describe_bins(cfg.bin_edges)
                for key, cfg in config.display_spectra.items()
                if cfg.bin_edges is not None}
    return flask.render_template('edit_settings.html', settings=settings,
                                 version_record=record, binnings=binnings,
                                 keys=keys, errors=errors), \
        400 if errors else 200


def compare_versions():
    """ Compare the active version with the version in the `with` query
    argument
    """
    tag = flask.g.active_version
    other = flask.request.args.get('with') or None
    if other is not None and not vc.version_exists(other):
        flask.abort(404, f"Version '{other}' does not exist")
    comparison = diff_versions(tag, other) if other else None
    settings_entries = []
    if comparison is not None:
        # the settings have no page of their own to show the differences
        settings_entries = compare_document(
            VersionSettings, SETTINGS_ORIGINAL_ID, tag, other).entries
    return flask.render_template('versions_compare.html', other=other,
                                 comparison=comparison,
                                 settings_entries=settings_entries,
                                 format_value=format_raw_value)


def export_version():
    """ Download the active version as a file """
    tag = flask.g.active_version
    job = appjobs.submit_and_wait('export_version', {'version': tag}, None)
    if job.status == 'succeeded':
        return appjobs.send_export(job)
    if job.status in job_models.FINISHED:
        raise job_error(job)
    return appjobs.waiting_page(job, flask.url_for('jobs.download',
                                                   job_id=str(job.id)))
