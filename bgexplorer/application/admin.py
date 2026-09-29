""" Site administration pages, for site_admin users only """
import io
import flask
from mongoengine.errors import ValidationError
from werkzeug.datastructures import MultiDict
from ..models import versioncontrol as vc
from ..models import maintenance
from ..models.settings import ApplicationSettings, get_application_settings
from .api import validation_fields
from .forms import update_object
from .auth import require, Role

# form fields the site settings editor may change; the logo is uploaded
APP_SETTINGS_FIELDS = ('org_name', 'org_url', 'allow_anon_view')
MAX_LOGO_SIZE = 1024 * 1024


def create_admin_blueprint() -> flask.Blueprint:
    bp = flask.Blueprint('admin', __name__)

    @bp.before_request
    def check_role():
        # the logo is shown on every page, including the login page
        if flask.request.endpoint != 'admin.logo':
            return require(Role.site_admin)
        return None

    @bp.get('/')
    def index():
        return flask.render_template('admin_index.html')

    @bp.route('/settings', methods=['GET', 'POST'])
    def settings():
        app_settings = get_application_settings()
        errors = {}
        if flask.request.method == 'POST':
            form = flask.request.form
            update_object(app_settings,
                          MultiDict([(k, v) for k, v in form.items(multi=True)
                                     if k in APP_SETTINGS_FIELDS]),
                          errors=errors)
            logo = flask.request.files.get('org_logo')
            if 'remove_logo' in form:
                app_settings.org_logo = None
            elif logo and logo.filename:
                data = logo.read()
                if len(data) > MAX_LOGO_SIZE:
                    errors['org_logo'] = "The logo must be smaller than 1 MB"
                elif not (logo.mimetype or '').startswith('image/'):
                    errors['org_logo'] = "The logo must be an image"
                else:
                    app_settings.org_logo = data
            if not errors:
                try:
                    app_settings.save()
                except ValidationError as e:
                    errors = validation_fields(e)
                else:
                    flask.flash("Site settings saved", 'success')
                    return flask.redirect(flask.url_for('.settings'))
        return flask.render_template('admin_settings.html',
                                     app_settings=app_settings,
                                     errors=errors), 400 if errors else 200

    @bp.get('/logo')
    def logo():
        app_settings = ApplicationSettings.objects.only('org_logo').first()
        if not app_settings or not app_settings.org_logo:
            flask.abort(404)
        data = app_settings.org_logo
        mimetype = 'image/svg+xml' if data.lstrip()[:1] == b'<' else None
        return flask.send_file(io.BytesIO(data),
                               mimetype=mimetype or _image_type(data))

    @bp.get('/maintenance')
    def maintenance_page():
        return flask.render_template('admin_maintenance.html',
                                     stats=maintenance.database_stats(),
                                     orphans=maintenance.find_orphans(),
                                     versions=vc.list_versions())

    @bp.post('/maintenance/clear-cache')
    def clear_cache():
        maintenance.clear_calculation_cache()
        flask.flash("Cleared the calculation cache", 'success')
        return flask.redirect(flask.url_for('.maintenance_page'))

    @bp.post('/maintenance/rebuild')
    def rebuild():
        version = flask.request.form.get('version', '')
        if not vc.version_exists(version):
            flask.abort(404, f"No version '{version}'")
        count = maintenance.rebuild_sourceterms(version)
        flask.flash(f"Rebuilt {count} source terms in '{version}'", 'success')
        return flask.redirect(flask.url_for('.maintenance_page'))

    @bp.post('/maintenance/delete-orphans')
    def delete_orphans():
        count = maintenance.delete_orphans()
        flask.flash(f"Removed or fixed {count} orphaned documents", 'success')
        return flask.redirect(flask.url_for('.maintenance_page'))

    return bp


def _image_type(data: bytes) -> str:
    """ Guess an image mimetype from its first bytes """
    for magic, mimetype in ((b'\x89PNG', 'image/png'),
                            (b'\xff\xd8', 'image/jpeg'),
                            (b'GIF8', 'image/gif'),
                            (b'RIFF', 'image/webp')):
        if data.startswith(magic):
            return mimetype
    return 'application/octet-stream'
