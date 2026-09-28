import flask
import mongoengine as me
from bson import ObjectId
from io import BytesIO
from ..models.sourceterm import find_sourceterms
from ..models.component import Component
from ..models.fields import InlineAttachment
from ..models.importexport import iter_json_documents, import_documents
from ..models.verdoc import check_writable
from .api import validation_fields
from .forms import update_object
from ..models.histogram import Histogram
from pint.errors import PintError
import json


def get_or_404(queryset, objid):
    try:
        return queryset.get(original_id=ObjectId(objid))
    except queryset._document.DoesNotExist:
        flask.abort(404, f"No {queryset._document._class_name} with id {objid}")


def flash_import_report(report) -> None:
    """ Flash a summary of an ImportReport """
    if report.created:
        flask.flash(f"Imported {len(report.created)} documents", 'success')
    if report.dropped_refs:
        flask.flash(flask.render_template_string(
            "Dropped references to documents not in this version:"
            "<ul>{% for label, ref in refs %}<li>{{ label }}: {{ ref }}</li>"
            "{% endfor %}</ul>", refs=report.dropped_refs), 'warning')
    if report.errors:
        flask.flash(flask.render_template_string(
            "Not imported:<ul>{% for label, error in errors %}"
            "<li>{{ label }}: {{ error }}</li>{% endfor %}</ul>",
            errors=report.errors), 'danger')
    if not (report.created or report.errors):
        flask.flash("Nothing to import", 'warning')


class CollectionViews(flask.Blueprint):
    # large fields that are left out of queries unless an endpoint asks for
    # them with `loads`
    DEFERRED_FIELDS = ('attachments__data', 'spectra')

    def __init__(self, doc_cls, **kwargs):
        self.doc_cls = doc_cls
        self.clsname = doc_cls.__name__.lower()
        self.has_attachments = hasattr(self.doc_cls, 'attachments')
        self.deferred_fields = [f for f in self.DEFERRED_FIELDS
                                if f.split('__')[0] in doc_cls._fields]
        # endpoint name: deferred fields it needs
        self._endpoint_fields = {}
        super().__init__(f'{self.clsname}', __name__, **kwargs)
        self.doc_cls.objects.__class__.get_or_404 = get_or_404
        self._setup_processing()
        self._create_endpoints()

    def loads(self, *fields):
        """ Decorator: the endpoint needs the given DEFERRED_FIELDS. Apply it
        below the route decorator.
        """
        def decorator(func):
            self._endpoint_fields[f'{self.name}.{func.__name__}'] = set(fields)
            return func
        return decorator

    @property
    def queryset(self):
        """ The active version, without the deferred fields that the
        current endpoint doesn't need
        """
        qs = self.doc_cls.select_version(flask.g.active_version)
        needed = self._endpoint_fields.get(flask.request.endpoint, ())
        if skip := [f for f in self.deferred_fields if f not in needed]:
            qs = qs.exclude(*skip)
        return qs

    def new_document(self, type_: str = None):
        """ A new, unsaved document in the active version. `type_` selects
        a subclass by lowercase name, e.g. 'assembly'
        """
        cls = self.doc_cls
        if type_:
            classes = {name.rsplit('.', 1)[-1].lower(): name
                       for name in self.doc_cls._subclasses}
            if type_.lower() not in classes:
                flask.abort(400, f"Unknown type '{type_}'")
            cls = me.base.get_document(classes[type_.lower()])
        return cls(version_tag=flask.g.active_version)

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
            try:
                response.last_modified = self.get_mtime(flask.g.objid)
            except:
                pass
            return response

        @self.context_processor
        def inject_bp_settings():
            return dict(doc_cls=self.doc_cls, clsname=self.clsname)

    def _create_endpoints(self):
        @self.get('/')
        def overview():
            data = self.queryset
            return flask.render_template(f'overview_{self.clsname}.html',
                                         cls=self.doc_cls, data=self.queryset)

        @self.get('/api/<objid>')
        @self.loads(*self.deferred_fields)
        def get_json():
            return flask.Response(flask.g.object.to_json(),
                                  mimetype='application/json')

        @self.get('/<objid>')
        def view():
            components = []
            if self.clsname == 'emissionspec':
                components = Component.select_version(flask.g.active_version)(specs=flask.g.object)
            return flask.render_template(f'view_{self.clsname}.html', components=components)

        @self.route('/new', methods=['GET', 'POST'])
        @self.route('/<objid>/edit', methods=['GET', 'POST'])
        def edit():
            check_writable(flask.g.active_version)
            req = flask.request
            errors = {}
            if 'object' not in flask.g:
                flask.g.object = self.new_document(req.args.get('type'))
            if req.method == 'POST':
                obj = update_object(flask.g.object, req.form, errors=errors)
                if not errors:
                    try:
                        obj.save()
                    except me.ValidationError as e:
                        errors = validation_fields(e)
                    else:
                        flask.flash(f"Successfully saved {obj}", 'success')
                        return flask.redirect(flask.url_for('.view',
                                                            object=obj))
            return flask.render_template(f'edit_{self.clsname}.html',
                                         form=flask.request.form,
                                         errors=errors), \
                400 if errors else 200

        @self.post('/<objid>/clone')
        @self.loads(*self.deferred_fields)
        def clone():
            check_writable(flask.g.active_version)
            original = flask.g.object
            copy = original.clone()
            if hasattr(copy, 'name'):
                copy.name = f"{original.name} (copy)"
            copy.save()
            flask.flash(f"Created {copy} as a copy of {original}", 'success')
            return flask.redirect(flask.url_for('.edit', object=copy))

        @self.route('/import', methods=['GET', 'POST'])
        def import_():
            check_writable(flask.g.active_version)
            if flask.request.method == 'POST':
                upload = flask.request.files.get('file')
                if not upload or not upload.filename:
                    flask.flash("Choose a file to import", 'danger')
                    return flask.render_template('import.html'), 400
                try:
                    docs = list(iter_json_documents(upload.stream,
                                                    upload.filename))
                except (ValueError, OSError) as e:
                    flask.flash(f"Can't read {upload.filename}: {e}",
                                'danger')
                    return flask.render_template('import.html'), 400
                report = import_documents(self.doc_cls, docs,
                                          flask.g.active_version)
                flash_import_report(report)
                return flask.redirect(flask.url_for('.overview'))
            return flask.render_template('import.html')

        if self.has_attachments:
            @self.route('/<objid>/attachments', methods=['GET', 'POST'])
            def attachments():
                if flask.request.method == 'POST':
                    pass
                return flask.render_template('attachments.html')

            @self.get('<objid>/attachments/<attachmentid>')
            def get_attachment(attachmentid):
                attachment = self.doc_cls.objects(id=flask.g.object.id).aggregate(
                    [{'$unwind': '$attachments'},
                     {'$replaceWith': '$attachments'},
                     {'$match': {'id': ObjectId(attachmentid)}},
                     ]).next()
                if not attachment:
                    flask.abort(404)
                return flask.send_file(BytesIO(attachment['data']),
                                       mimetype=attachment.get('mimetype'),
                                       download_name=attachment.get('filename'),
                                       etag=attachment.get('etag'),
                                       last_modified=attachment['id'].generation_time)


            @self.post('<objid>/attachments/add')
            def add_attachments():
                check_writable(flask.g.active_version)
                _file = flask.request.files['fupload']
                description = flask.request.form['description']
                attachment = InlineAttachment(data=_file.read(),
                                              filename=_file.filename,
                                              mimetype=_file.mimetype,
                                              description=description,
                                              )
                attachment.clean()
                flask.g.object.modify(push__attachments=attachment)
                return flask.redirect(flask.url_for('.attachments',
                                                    object=flask.g.object))




        if 'spectra' in self.doc_cls._fields:
            self._create_spectra_endpoints()

        @self.get('/<objid>/sourceterms')
        def sourceterms():
            sourceterms = find_sourceterms(flask.g.object)
            return flask.render_template('view_sourceterms.html',
                                         sourceterms=sourceterms)

    def _create_spectra_endpoints(self):
        """ Import, rename and delete the spectra of a HitEfficiency. Each
        redirects to the view page with a flashed result
        """
        errors = (KeyError, ValueError, me.ValidationError, PintError)

        def done(message=None, error=None):
            if error is not None:
                message = error.args[0] if error.args else str(error)
                flask.flash(str(message), 'danger')
            else:
                flask.flash(message, 'success')
            return flask.redirect(flask.url_for('.view', object=flask.g.object,
                                                _anchor='spectra'))

        @self.post('/<objid>/spectra/import')
        @self.loads('spectra')
        def import_spectrum():
            check_writable(flask.g.active_version)
            form = flask.request.form
            upload = flask.request.files.get('file')
            name = form.get('name', '').strip()
            if not upload or not upload.filename:
                return done(error=ValueError("Choose a file to import"))
            name = name or upload.filename.rsplit('.', 1)[0]
            try:
                hist = parse_spectrum(upload.read(), upload.filename,
                                      form.get('units') or None,
                                      form.get('binsunit') or None)
                flask.g.object.add_spectrum(name, hist,
                                            overwrite='overwrite' in form)
            except errors as e:
                return done(error=e)
            return done(f"Imported spectrum '{name}'")

        @self.post('/<objid>/spectra/rename')
        @self.loads('spectra')
        def rename_spectrum():
            check_writable(flask.g.active_version)
            form = flask.request.form
            try:
                flask.g.object.rename_spectrum(form.get('name', ''),
                                               form.get('newname', ''))
            except errors as e:
                return done(error=e)
            return done(f"Renamed spectrum '{form['name']}' to "
                        f"'{form['newname'].strip()}'")

        @self.post('/<objid>/spectra/delete')
        @self.loads('spectra')
        def delete_spectrum():
            check_writable(flask.g.active_version)
            name = flask.request.form.get('name', '')
            try:
                flask.g.object.remove_spectrum(name)
            except errors as e:
                return done(error=e)
            return done(f"Deleted spectrum '{name}'")


def parse_spectrum(data: bytes, filename: str = '', units=None,
                   binsunit=None) -> Histogram:
    """ Read a Histogram from histogram JSON or CSV/TXT columns. `units` and
    `binsunit` are defaults for JSON and required units for columns
    """
    text = data.decode('utf-8-sig')
    if filename.lower().endswith('.json') or text.lstrip().startswith('{'):
        try:
            d = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Not valid JSON: {e}")
        return Histogram.from_dict(d, units, binsunit)
    return Histogram.from_columns(text, units, binsunit)
