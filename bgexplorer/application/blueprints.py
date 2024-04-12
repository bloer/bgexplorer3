import flask
import mongoengine as me
from bson import ObjectId
from ..models.sourceterm import find_sourceterms
from .forms import update_object


def get_or_404(queryset, objid):
    try:
        return queryset.get(original_id=ObjectId(objid))
    except queryset._document.DoesNotExist:
        flask.abort(404, f"No {queryset._document._class_name} with id {objid}")


def _drill_down(obj, name, value, delimiter='__'):
    names = name.split(delimiter)
    sub_obj = obj
    for subname in names[:-1]:
        if isinstance(sub_obj, list):
            index = int(subname)
            sub_obj = sub_obj[index]
        else:
            sub_obj = getattr(sub_obj, subname)


class CollectionViews(flask.Blueprint):
    def __init__(self, doc_cls, **kwargs):
        self.doc_cls = doc_cls
        self.clsname = doc_cls.__name__.lower()
        self.has_attachments = hasattr(self.doc_cls, 'attachments')
        self.has_spectra = hasattr(self.doc_cls, 'spectra')
        super().__init__(f'{self.clsname}', __name__, **kwargs)
        self.doc_cls.objects.__class__.get_or_404 = get_or_404
        self._setup_processing()
        self._create_endpoints()

    @property
    def queryset(self):
        qs = self.doc_cls.select_version(flask.g.active_version)
        # exclude large attributes unless specifically requested
        if self.has_attachments:
            qs = qs.exclude('attachments__data')
        if self.has_spectra and 'spectr' not in flask.request.base_url:
            qs = qs.exclude('spectra')
        return qs

    def get_mtime(self, objid):
        obj = self.queryset.only('version_tags', 'original_id', 'modified')\
            .get_or_404(objid)
        mtime = find_sourceterms(obj).scalar('modified').first()
        return mtime or obj.modified

    def _setup_processing(self):
        """ Create URL preprocessing rules """
        @self.url_value_preprocessor
        def pop_objid(endpoint, values):
            flask.g.objid = values.pop('objid', None)

        @self.url_defaults
        def get_objid(endpoint, values):
            if obj := values.pop('object', None):
                values['objid'] = str(obj.original_id)
            # elif 'object' in flask.g and 'objid' not in values:
            #     values['objid'] = str(flask.g.object.original_id)
            if ((relativeto := values.get('relativeto')) and
                    not isinstance(relativeto, str)):
                values['relativeto'] = str(relativeto.original_id)

        @self.before_request
        def load_object():
            if objid := flask.g.get('objid'):
                # if self.test_cache(g.objid):
                #    return ("Not Modified", 304)
                flask.g.object = self.queryset.get_or_404(objid)
            if relativeto := flask.request.args.get('relativeto'):
                flask.g.relativeto = self.queryset.get_or_404(relativeto)

        @self.after_request
        def set_mtime(response):
            if 'object' in flask.g:
                response.last_modified = self.get_mtime(flask.g.objid)
            return response

    def _create_endpoints(self):
        @self.get('/')
        def overview():
            data = self.queryset
            return flask.render_template(f'overview_{self.clsname}.html',
                                         cls=self.doc_cls, data=self.queryset)

        @self.get('/api/<objid>')
        def get_json():
            return flask.Response(flask.g.object.to_json(),
                                  mimetype='application/json')

        @self.get('/<objid>')
        def view():
            return flask.render_template(f'view_{self.clsname}.html')

        @self.route('/<objid>/edit', methods=['GET', 'POST'])
        def edit():
            req = flask.request
            if req.form and req.method == 'POST':
                update_object(flask.g.object, req.form)
                return flask.Response(flask.g.object.to_json(),
                                     mimetype='application/json')
            return flask.render_template(f'edit_{self.clsname}.html',
                                         form=flask.request.form)

        if self.has_attachments:
            @self.route('/<objid>/attachments', methods=['GET', 'POST'])
            def attachments():
                if flask.request.method == 'POST':
                    pass
                return flask.render_template('attachments.html')

            @self.get('<objid>/attachments/<index>')
            def get_attachment(index):
                abort(404)
                data = self.doc_cls.objects(id=flask.g.object.id).aggregate(
                    [{'$project': {'$data': f'attachments.{index}'}}]
                    )

