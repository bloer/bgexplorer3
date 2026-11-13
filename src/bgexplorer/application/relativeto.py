""" The Component that source terms and results are shown "relative to".

It is the `relativeto` query argument, so a link shows what its sender saw.
Pages that show source terms and results resolve it into `g.relativeto` (only
set when given), and links to the pages in `STICKY` carry it along until it is
changed or cleared, which a link does with an explicit `relativeto=None`.
"""
import flask
from bson import ObjectId
from bson.errors import InvalidId
from ..models.component import Component

# blueprints whose pages show things relative to a component
BLUEPRINTS = ('component', 'emissionspec', 'hitefficiency')
# endpoints in those blueprints whose links keep the component
STICKY = ('overview', 'view', 'sourceterms', 'results', 'results_table',
          'dashboard_json', 'spectrum_json', 'choose_relativeto')
# the pages that a component can be chosen for, and are returned to after
TARGETS = ('view', 'sourceterms', 'results')


def init_app(app: flask.Flask) -> None:
    @app.before_request
    def load_relativeto():
        relativeto = flask.request.args.get('relativeto')
        tag = flask.g.get('active_version')
        if (not relativeto or tag is None or
                flask.request.blueprint not in BLUEPRINTS):
            return
        try:
            flask.g.relativeto = Component.select_version(tag).exclude(
                'attachments__data').get(original_id=ObjectId(relativeto))
        except (InvalidId, Component.DoesNotExist):
            flask.abort(404, f"No component with id {relativeto} in {tag}")

    @app.url_defaults
    def add_relativeto(endpoint, values):
        if 'relativeto' in values or 'relativeto' not in flask.g:
            return
        blueprint, _, name = endpoint.partition('.')
        if blueprint not in BLUEPRINTS or name not in STICKY:
            return
        # another version may not have the component
        if (values.get('active_version', flask.g.get('active_version'))
                != flask.g.get('active_version')):
            return
        values['relativeto'] = str(flask.g.relativeto.original_id)
