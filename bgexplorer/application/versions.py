""" HTML pages to create and delete versions """
import flask
from ..models import versioncontrol as vc
from ..models.verdoc import VersionedDocument
from .api import APIError, validate_new_version


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

    @bp.route('/<path:active_version>/delete', methods=['GET', 'POST'])
    def delete():
        tag = flask.g.active_version
        protected = tag == VersionedDocument.get_default_tag()
        if flask.request.method == 'POST':
            # the model refuses to delete the default version
            vc.delete_version(tag)
            flask.flash(f"Deleted version '{tag}'", 'success')
            return flask.redirect(flask.url_for('index'))
        return flask.render_template('versions_delete.html',
                                     summary=vc.version_summary(tag),
                                     protected=protected)

    return bp
