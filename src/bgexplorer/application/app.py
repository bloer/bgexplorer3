import flask
import hashlib
import os
import time
from flask_login import current_user
from flask_bootstrap import Bootstrap5
from flask_wtf.csrf import CSRFProtect
from werkzeug.exceptions import HTTPException
import mongoengine
import enum
import logging
import importlib
from ..models.settings import (get_settings, get_application_settings,
                               get_secret_key, ApplicationSettings)
from ..versioning.record import get_record, get_cache_token
from ..models.common import UnitMismatchError
from ..models.component import Component
from ..models.emissionspec import EmissionSpec
from ..models.hiteff import HitEfficiency
from ..models.cosmogenic import ActivatedMaterial
from ..models.fields import get_fromstr
from .common import pretty_date
from .blueprints import CollectionViews
from .api import create_api, API_VERSION
from .versions import (create_versions_blueprint, edit_settings,
                       compare_versions, export_version)
from .admin import create_admin_blueprint
from .radiopurity import create_radiopurity_blueprint
from ..versioning.control import list_versions, version_exists
from ..versioning.verdoc import VersionedDocument
from ..versioning.history import recent_events
from .forms import input_type, input_value, field_kind
from . import auth

from ..models.asymmetric import AsymmetricUncertainty
import pint
log = logging.getLogger(__name__)


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
    app.db = mongoengine.connect(host=app.config.get('MONGODB_URI'))
    if not app.config.get('SECRET_KEY'):
        log.warning("No SECRET_KEY is configured; using the one generated "
                    "and stored in the database")
        app.config['SECRET_KEY'] = get_secret_key()

    # app extensions
    Bootstrap5(app)
    csrf = CSRFProtect(app)

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
    app.register_blueprint(create_radiopurity_blueprint(),
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

    # Pages and json under /explore/<version>/ carry an ETag, and are answered
    # with 304 before rendering if the browser's copy is still good. Static
    # files are cached for a long time under a url that changes with the file.
    started = time.time()
    app_version = importlib.metadata.version('bgexplorer')
    csrf_limit = app.config.get('WTF_CSRF_TIME_LIMIT', 3600)
    # the browser may keep the page but must ask before using it; private
    # because the page depends on who is logged in
    cache_control = 'private, no-cache'
    # static has its own caching, downloads send their own validators
    uncached = {'static', 'export_version', 'get_attachment'}

    def page_state():
        """ What the cacheable page depends on besides its url and csrf
        token, or None if the page isn't cacheable
        """
        request = flask.request
        if (request.method not in ('GET', 'HEAD')
                or not flask.g.get('active_version') or not request.endpoint
                or request.endpoint.rsplit('.', 1)[-1] in uncached
                # a 304 would swallow the flash message
                or '_flashes' in flask.session):
            return None
        tags = [flask.g.active_version,
                (request.view_args or {}).get('other_version'),
                request.args.get('with')]
        tokens = [get_cache_token(tag) for tag in tags if tag]
        if tokens[0] is None:
            return None
        return [tokens, current_user.get_id(), auth.current_role(),
                auth.needs_setup(), org_branding(), app_version, started]

    def etag_of(state):
        # the form tokens in the page expire, and a 304 keeps the old page
        bucket = csrf_limit and int(time.time() // (csrf_limit // 2))
        return hashlib.sha1(repr([state, bucket, flask.session.get(
            'csrf_token')]).encode()).hexdigest()

    @app.before_request
    def check_etag():
        flask.g.etag_state = state = page_state()
        if state is None:
            return None
        etag = etag_of(state)
        if flask.request.if_none_match.contains_weak(etag):
            response = flask.Response(status=304)
            response.set_etag(etag)
            response.headers['Cache-Control'] = cache_control
            return response
        return None

    @app.after_request
    def set_etag(response):
        state = flask.g.get('etag_state')
        if (state is not None and response.status_code == 200
                and not response.direct_passthrough
                and 'ETag' not in response.headers):
            # computed again: rendering may have started the csrf token
            response.set_etag(etag_of(state))
            response.headers['Cache-Control'] = cache_control
        return response

    @app.url_defaults
    def version_static_url(endpoint, values):
        if endpoint == 'static' and 'filename' in values and 'v' not in values:
            try:
                values['v'] = int(os.path.getmtime(
                    os.path.join(app.static_folder, values['filename'])))
            except OSError:
                pass

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

    @app.errorhandler(UnitMismatchError)
    def unit_mismatch(e):
        """ Saved data whose units don't fit together (the audit after an
        import or merge reports these, see `check_version_units`)
        """
        message = f"Units in the saved data don't match: {e}"
        if wants_json() or flask.request.path.endswith('.json'):
            return flask.jsonify(error=dict(message=message)), 500
        return flask.render_template('error.html', title="Unit mismatch",
                                     message=message), 500

    @app.template_global()
    def bgexplorer_version():
        return importlib.metadata.version('bgexplorer')

    @app.template_global()
    def all_source_names():
        return (EmissionSpec.select_version(flask.g.active_version)
                .distinct('sources.name'))

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
            q = q.to_reduced_units()
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
            version_record = get_record(tag)
            settings = get_settings(tag, create=False)
        except KeyError:
            return dict()
        # whether to show buttons and forms that change this version
        return dict(settings=settings, version_record=version_record,
                    can_edit=(version_record.editable
                              and version_record.active_lock is None
                              and auth.has_role('editor')))

    @app.template_global()
    def has_diff_page(cls):
        return f'{cls.__name__.lower()}.diff' in app.view_functions

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
        # requested without the ?v= of other static files, so not for a year
        return flask.send_from_directory(app.static_folder, 'favicon.ico',
                                         max_age=86400)

    @app.get('/explore/<active_version>')
    def overview():
        return flask.render_template(
            'overview.html',
            events=recent_events(flask.g.active_version))

    app.add_url_rule('/explore/<active_version>/settings',
                     'edit_settings', edit_settings, methods=['GET', 'POST'])
    app.add_url_rule('/explore/<active_version>/compare',
                     'compare_versions', compare_versions)
    app.add_url_rule('/explore/<active_version>/export',
                     'export_version', export_version)

    @app.get('/explore/<active_version>/hitefflocations')
    def hitefflocations():
        """ Return a list of all 'location' keys in the HitEfficiency DB """
        return flask.jsonify(HitEfficiency.select_version(flask.g.active_version)
                             .distinct('location'))

    return app
