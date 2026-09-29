import flask
from flask_bootstrap import Bootstrap5
from flask_wtf.csrf import CSRFProtect
from werkzeug.exceptions import HTTPException
import mongoengine
import secrets
import enum
import importlib
from ..models.settings import (get_settings, get_application_settings,
                               ApplicationSettings)
from ..models.component import Component
from ..models.emissionspec import EmissionSpec
from ..models.hiteff import HitEfficiency
from ..models.cosmogenic import ActivatedMaterial
from ..models.sourceterm import CalculatedResults
from ..models.fields import get_fromstr
from .common import pretty_date
from .blueprints import CollectionViews
from .api import create_api, API_VERSION
from .versions import create_versions_blueprint, edit_settings
from .admin import create_admin_blueprint
from ..models.versioncontrol import list_versions, version_exists
from ..models.verdoc import VersionedDocument
from .forms import input_type, input_value, field_kind
from . import examples
from . import auth

from ..models.asymmetric import AsymmetricUncertainty
import pint


def create_app(config_file=None, config=None):
    """ Create the flask app. Configuration is read in order from
    config_default, the file named by the BGEXPLORER_CONFIG environment
    variable, `config_file`, FLASK_* environment variables, and the
    `config` mapping, with later sources overriding earlier ones
    """
    app = flask.Flask(__name__)
    # app configuration
    app.config.from_object('bgexplorer.application.config_default')
    app.config.from_envvar('BGEXPLORER_CONFIG', silent=True)
    if config_file:
        app.config.from_pyfile(config_file)
    app.config.from_prefixed_env()
    if config:
        app.config.update(config)
    if not app.config.get('SECRET_KEY'):
        if not (app.debug or app.testing):
            raise RuntimeError(
                "SECRET_KEY must be set, e.g. with the FLASK_SECRET_KEY "
                "environment variable, since it protects logins")
        # sessions end when the server restarts
        app.config['SECRET_KEY'] = secrets.token_hex()

    # app extensions
    Bootstrap5(app)
    csrf = CSRFProtect(app)
    app.db = mongoengine.connect(host=app.config.get('MONGODB_URI'))

    # make sure application settings and default version exist
    get_application_settings()
    get_settings()

    # first, so that logins are checked before anything else
    auth.init_app(app)

    # blueprints
    # the API only accepts JSON bodies, which can't be sent cross-site
    # without CORS, so doesn't need CSRF tokens
    api = create_api()
    csrf.exempt(api)
    app.register_blueprint(api, url_prefix=f'/api/{API_VERSION}')
    app.register_blueprint(create_versions_blueprint(),
                           url_prefix='/versions')
    app.register_blueprint(create_admin_blueprint(), url_prefix='/admin')
    app.register_blueprint(CollectionViews(Component),
                           url_prefix='/explore/<active_version>/component')
    app.register_blueprint(CollectionViews(EmissionSpec),
                           url_prefix='/explore/<active_version>/emission')
    app.register_blueprint(CollectionViews(HitEfficiency),
                           url_prefix='/explore/<active_version>/hiteff')
    app.register_blueprint(CollectionViews(ActivatedMaterial),
                           url_prefix='/explore/<active_version>/activation')
    # app preprocessing
    @app.url_defaults
    def add_active_version(endpoint, values):
        if 'active_version' in values or not flask.g.get('active_version'):
            return
        if app.url_map.is_endpoint_expecting(endpoint, 'active_version'):
            values['active_version'] = flask.g.active_version

    @app.url_value_preprocessor
    def pull_active_version(endpoint, values):
        if values:
            flask.g.active_version = values.pop('active_version', None)

    @app.before_request
    def check_active_version():
        # the API reports missing versions itself
        if flask.request.blueprint == 'api':
            return
        tag = flask.g.get('active_version')
        if tag is not None and not version_exists(tag):
            flask.abort(404, f"Version '{tag}' does not exist")

    def wants_json():
        return flask.request.path.startswith('/api/')

    @app.errorhandler(HTTPException)
    def http_error(e):
        if wants_json():
            return flask.jsonify(error=dict(message=e.description)), e.code
        return flask.render_template('error.html', title=f"{e.code} {e.name}",
                                     message=e.description), e.code

    @app.errorhandler(PermissionError)
    def permission_error(e):
        """ e.g. trying to edit a read-only version """
        if wants_json():
            return flask.jsonify(error=dict(message=str(e))), 403
        return flask.render_template('error.html', title="Not allowed",
                                     message=str(e)), 403

    @app.template_global()
    def bgexplorer_version():
        return importlib.metadata.version('bgexplorer')

    @app.template_global()
    def all_source_names():
        return (EmissionSpec.select_version(flask.g.active_version)
                .distinct('sources.name'))

    @app.template_global()
    def get_calculation(obj, relativeto=None):
        return CalculatedResults.for_object(obj, relativeto, save=True)

    @app.template_global()
    def get_tree_calculations(root, spectra=False):
        """ results for root and all its subcomponents relative to root,
        keyed by component original_id
        """
        return CalculatedResults.for_tree(root, spectra=spectra)

    @app.template_filter('sourcesort')
    def source_sort_val(rate):
        try:
            rate = rate.to('Bq/kg')
        except pint.errors.DimensionalityError:
            pass
        rate = rate.m
        if rate.isupperlimit():
            return rate.ppf(0.9)
        return rate.mode

    @app.template_filter('printquantity')
    def printquantity(q, unit=None):
        if fromstr := get_fromstr(q):
            return fromstr
        if unit:
            q = q.to(unit)
        else:
            q = q.to_compact()
        return '{:.2g~P}'.format(q)

    @app.template_filter('printstring')
    def printstring(val):
        if fromstr := get_fromstr(val):
            return fromstr
        if val is None:
            return ''
        if isinstance(val, enum.Enum):
            return str(val.value)
        return str(val)

    @app.template_global()
    def org_branding():
        """ Organization name, url and whether a logo is set, without
        loading the logo itself
        """
        doc = ApplicationSettings._get_collection().find_one(
            {}, {'org_name': 1, 'org_url': 1,
                 'has_logo': {'$gt': ['$org_logo', None]}})
        doc = doc or {}
        return dict(name=doc.get('org_name'), url=doc.get('org_url'),
                    has_logo=bool(doc.get('has_logo')))

    @app.template_global()
    def all_versions():
        return list_versions().only('version_tag', 'editable')

    @app.context_processor
    def inject_settings():
        tag = flask.g.get('active_version')
        if tag is None:
            return dict()
        try:
            settings = get_settings(tag, create=False)
        except KeyError:
            return dict()
        # whether to show buttons and forms that change this version
        return dict(settings=settings,
                    can_edit=settings.editable and auth.has_role('editor'))

    app.add_template_global(pretty_date, 'pretty_date')
    app.add_template_filter(pretty_date, 'pretty_date')
    app.add_template_global(input_type, 'input_type')
    app.add_template_global(input_value, 'input_value')
    app.add_template_global(field_kind, 'field_kind')
    app.add_template_global(get_fromstr, 'get_fromstr')
    app.add_template_global(VersionedDocument.get_default_tag(),
                            'default_version')

    # app endpoints
    @app.get('/')
    def index():
        return flask.render_template("index.html", versions=list_versions())

    @app.get('/favicon.ico')
    def favicon():
        return app.send_static_file('favicon.ico')

    @app.get('/explore/<active_version>')
    def overview():
        return flask.render_template('overview.html')

    app.add_url_rule('/explore/<active_version>/settings',
                     'edit_settings', edit_settings, methods=['GET', 'POST'])

    @app.get('/explore/<active_version>/hitefflocations')
    def hitefflocations():
        """ Return a list of all 'location' keys in the HitEfficiency DB """
        return flask.jsonify(HitEfficiency.select_version(flask.g.active_version)
                             .distinct('location'))

    #examples.qis.populate_example(clean=True)
    @app.get('/test')
    def test():
        return """<select value="x"></select>"""

    return app
