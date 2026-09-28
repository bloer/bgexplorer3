""" HTML pages to create and delete versions and edit their settings """
import flask
from mongoengine.errors import ValidationError
from werkzeug.datastructures import MultiDict
from ..models import versioncontrol as vc
from ..models.verdoc import VersionedDocument, check_writable
from ..models.settings import get_settings
from ..models.hiteff import HitEfficiency
from .api import APIError, validate_new_version, validation_fields
from .forms import update_object, LISTFIELDS_KEY


def create_versions_blueprint() -> flask.Blueprint:
    bp = flask.Blueprint('versions', __name__)

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
