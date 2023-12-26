import flask
from flask_bootstrap import Bootstrap5
import mongoengine
import secrets
import importlib
from ..models.settings import (get_settings, get_application_settings,
                               VersionSettings)
from ..models.component import Component
from ..models.emissionspec import EmissionSpec
from ..models.hiteff import HitEfficiency
from .common import pretty_date
from .blueprints import CollectionViews
from . import examples

from ..models.asymmetric import AsymmetricUncertainty
import pint


def create_app(config_file=None):
    app = flask.Flask(__name__)
    # app configuration
    app.config.from_object('bgexplorer.application.config_default')
    app.config.from_envvar('BGEXPLORER_CONFIG', silent=True)
    if config_file:
        app.config.from_pyfile(config_file)
    app.config.from_prefixed_env()
    # this is a really bad idea
    if 'SECRET_KEY' not in app.config:
        app.config['SECRET_KEY'] = secrets.token_hex()

    # app extensions
    Bootstrap5(app)
    app.db = mongoengine.connect(host=app.config.get('MONGODB_URI'))

    # make sure application settings and default version exist
    get_application_settings()
    get_settings()

    # blueprints
    app.register_blueprint(CollectionViews(Component),
                           url_prefix='/<active_version>/component')
    app.register_blueprint(CollectionViews(EmissionSpec),
                           url_prefix='/<active_version>/emission')
    app.register_blueprint(CollectionViews(HitEfficiency),
                           url_prefix='/<active_version>/hiteff')
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

    @app.template_global()
    def bgexplorer_version():
        return importlib.metadata.version('bgexplorer')

    @app.context_processor
    def inject_settings():
        try:
            return dict(settings=get_settings(flask.g.active_version))
        except AttributeError:
            return dict()

    app.add_template_global(pretty_date, 'pretty_date')

    # app endpoints
    @app.get('/')
    def index():
        branches = VersionSettings.objects(editable=True)
        tags = VersionSettings.objects(editable=False)
        return flask.render_template("index.html", branches=branches,
                                     tags=tags)

    @app.get('/favicon.ico')
    def favicon():
        return flask.send_static_file('favicon.ico')

    @app.get('/explore/<active_version>')
    def overview():
        return flask.render_template('overview.html')

    examples.qis.populate_example(clean=True)

    return app
