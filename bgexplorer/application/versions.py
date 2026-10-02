""" HTML pages to create and delete versions and edit their settings """
import flask
from mongoengine.errors import ValidationError
from werkzeug.datastructures import MultiDict
from ..models import versioncontrol as vc
from ..models.verdoc import VersionedDocument, check_writable
from ..models.settings import get_settings, release_lock
from ..models.history import EventAction, log_event
from ..models.versiondiff import diff_versions
from ..models.hiteff import HitEfficiency
from .api import APIError, validate_new_version, validation_fields
from .forms import update_object, LISTFIELDS_KEY
from .blueprints import format_raw_value
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

    @bp.route('/<active_version>/delete', methods=['GET', 'POST'])
    def delete():
        tag = flask.g.active_version
        settings = get_settings(tag)
        protected = (tag == VersionedDocument.get_default_tag()
                     or not settings.editable)
        if flask.request.method == 'POST':
            # the model refuses to delete the default version and tags
            vc.delete_version(tag, allow_tags=False)
            flask.flash(f"Deleted version '{tag}'", 'success')
            return flask.redirect(flask.url_for('index'))
        return flask.render_template('versions_delete.html',
                                     summary=vc.version_summary(tag),
                                     protected=protected,
                                     istag=not settings.editable)

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
            if req.method == 'POST':
                try:
                    done = vc.merge_version(source, tag, rule,
                                            form.get('fingerprint') or None)
                except vc.MergeError as e:
                    error, status = e, 409
                else:
                    flask.flash(f"Merged '{source}' into '{tag}': "
                                f"{vc._merge_summary(done)}"
                                if done.changes else
                                f"Nothing to merge from '{source}'",
                                'success')
                    return flask.redirect(flask.url_for('overview'))
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
        if release_lock(tag):
            log_event(EventAction.clear_lock, tag)
            flask.flash(f"Cleared the lock on '{tag}'", 'success')
        return flask.redirect(flask.url_for('overview'))

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
    """ Edit the VersionSettings of the active version """
    tag = flask.g.active_version
    check_writable(tag)
    settings = get_settings(tag, create=False)
    errors = {}
    if flask.request.method == 'POST':
        update_object(settings, _settings_form(flask.request.form),
                      errors=errors)
        if not errors:
            try:
                settings.save()
            except ValidationError as e:
                errors = validation_fields(e)
            else:
                flask.flash("Settings saved", 'success')
                return flask.redirect(flask.url_for('overview'))
    # show display settings for every key in the hit efficiencies
    config = settings.hiteffdbconfig
    hiteffs = HitEfficiency.select_version(tag)
    keys = {type_: list(dict.fromkeys(
                list(getattr(config, f'display_{type_}'))
                + sorted(hiteffs.distinct(f'{type_}_keys'))))
            for type_ in ('scalars', 'spectra')}
    return flask.render_template('edit_settings.html', settings=settings,
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
    return flask.render_template('versions_compare.html', other=other,
                                 comparison=comparison,
                                 format_value=format_raw_value)
