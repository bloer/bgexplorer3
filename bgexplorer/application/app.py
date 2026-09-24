import flask
from flask_bootstrap import Bootstrap5
import mongoengine
import secrets
import enum
import importlib
from ..models.settings import (get_settings, get_application_settings,
                               VersionSettings)
from ..models.component import Component
from ..models.emissionspec import EmissionSpec
from ..models.hiteff import HitEfficiency
from ..models.sourceterm import CalculatedResults
from ..models.fields import get_fromstr
from .common import pretty_date
from .blueprints import CollectionViews
from .forms import input_type, input_value
from . import examples

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
                           url_prefix='/<path:active_version>/component')
    app.register_blueprint(CollectionViews(EmissionSpec),
                           url_prefix='/<path:active_version>/emission')
    app.register_blueprint(CollectionViews(HitEfficiency),
                           url_prefix='/<path:active_version>/hiteff')
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

    @app.context_processor
    def inject_settings():
        try:
            return dict(settings=get_settings(flask.g.active_version))
        except AttributeError:
            return dict()

    app.add_template_global(pretty_date, 'pretty_date')
    app.add_template_filter(pretty_date, 'pretty_date')
    app.add_template_global(input_type, 'input_type')
    app.add_template_global(input_value, 'input_value')
    app.add_template_global(get_fromstr, 'get_fromstr')

    # app endpoints
    @app.get('/')
    def index():
        branches = VersionSettings.objects(editable=True)
        tags = VersionSettings.objects(editable=False)
        return flask.render_template("index.html", branches=branches,
                                     tags=tags)

    @app.get('/favicon.ico')
    def favicon():
        return app.send_static_file('favicon.ico')

    @app.get('/explore/<path:active_version>')
    def overview():
        return flask.render_template('overview.html')

    @app.get('/explore/<path:active_version>/hitefflocations')
    def hitefflocations():
        """ Return a list of all 'location' keys in the HitEfficiency DB """
        return flask.jsonify(HitEfficiency.select_version(flask.g.active_version)
                             .distinct('location'))

    #examples.qis.populate_example(clean=True)
    @app.get('/test')
    def test():
        return """<select value="x"></select>"""

    return app
