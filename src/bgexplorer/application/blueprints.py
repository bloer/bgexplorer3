import flask
import mongoengine as me
from bson import ObjectId
from bson.errors import InvalidId
from io import BytesIO
from ..models.sourceterm import find_sourceterms, CalculatedResults
from ..models.budget import (available_scalars, GROUPBY, dashboard, table,
                             spectrum_breakdown, spectrum_names,
                             cached_filtered,
                             BudgetFilter)
from ..models.component import Component, Assembly
from ..models.emissionspec import EmissionSpec
from ..models.hiteff import HitEfficiency
from ..models.cosmogenic import ActivatedMaterial, CosmogenicActivation
from ..models.fields import InlineAttachment
from ..models.importexport import iter_json_documents, import_documents
from ..models.verdoc import check_writable
from ..models.versiondiff import compare_document, find_refs
from ..models.versioncontrol import (version_exists, import_document,
                                     VersionControlError)
from ..models.settings import get_settings
from .api import validation_fields
from .auth import require_for_changes, Role
from .forms import update_object, list_snapshot, edited_rows
from ..models.histogram import Histogram
from pint.errors import PintError
from .plotting import histogram_json, scalar_json, unit_str
import json
import markupsafe


def get_or_404(queryset, objid):
    try:
        return queryset.get(original_id=ObjectId(objid))
    except queryset._document.DoesNotExist:
        flask.abort(404, f"No {queryset._document._class_name} with id {objid}")


# versioned classes that documents refer to, and their blueprints
REFERENCED_CLASSES = ((Component, 'component'),
                      (EmissionSpec, 'emissionspec'),
                      (HitEfficiency, 'hitefficiency'),
                      (ActivatedMaterial, 'activatedmaterial'))


def describe_refs(ids, version: str) -> dict:
    """ {original_id: (blueprint, name)} for the documents with `ids` in
    `version`
    """
    found = {}
    ids = list(set(ids))
    for cls, endpoint in REFERENCED_CLASSES:
        if not ids:
            break
        for doc in cls._get_collection().find(
                {'version_tags': version, 'original_id': {'$in': ids}},
                {'original_id': 1, 'name': 1, 'source': 1, 'location': 1}):
            name = doc.get('name') or ' @ '.join(
                str(doc[k]) for k in ('source', 'location') if doc.get(k))
            found[doc['original_id']] = (endpoint, name)
        ids = [i for i in ids if i not in found]
    return found


def format_raw_value(value, refs: dict, version: str, maxlen: int = 200):
    """ HTML for a raw (`to_mongo`) value in a diff. Ids are looked up in
    `refs` (see describe_refs) and linked to `version`
    """
    esc = markupsafe.escape
    if value is None or value == []:
        return markupsafe.Markup('<span class="text-secondary">none</span>')
    if isinstance(value, ObjectId):
        if value not in refs:
            return markupsafe.Markup('<span class="text-danger" title="Not '
                                     'in this version">missing {}</span>'
                                     ).format(value)
        endpoint, name = refs[value]
        url = flask.url_for(f'{endpoint}.view', objid=str(value),
                            active_version=version)
        return markupsafe.Markup('<a href="{}">{}</a>').format(url, name)
    if isinstance(value, dict):
        if 'str' in value:
            return esc(value['str'])
        if 'units' in value:
            return esc(f"{value.get('value')} {value['units']}")
        items = [markupsafe.Markup('<dt class="col-4">{}</dt>'
                                   '<dd class="col-8">{}</dd>').format(
                     key, format_raw_value(item, refs, version, maxlen))
                 for key, item in value.items() if key not in ('id', '_cls')]
        return markupsafe.Markup('<dl class="row mb-0">{}</dl>').format(
            markupsafe.Markup('').join(items))
    if isinstance(value, list):
        return markupsafe.Markup(', ').join(
            format_raw_value(item, refs, version, maxlen) for item in value)
    text = str(value)
    if len(text) > maxlen:
        text = text[:maxlen] + '…'
    return esc(text)


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

    def delete_impact(self, obj) -> list:
        """ What else changes in the active version when `obj` is deleted:
        a list of dicts with a `text` and optionally the `docs` it lists and
        the `endpoint` blueprint to link them
        """
        if isinstance(obj, Component):
            impact = [dict(text="Removed from these assemblies:",
                           docs=list(obj.find_parents()),
                           endpoint='component')]
            if owned := list(EmissionSpec.select_version(
                    obj.active_version)(owner=obj)):
                impact.append(dict(text="Its own specs are deleted:",
                                   docs=owned, endpoint='emissionspec'))
            if isinstance(obj, Assembly):
                impact.append(dict(text="Its children are kept."))
            return impact
        if isinstance(obj, EmissionSpec):
            return [dict(text="Removed from these components:",
                         docs=list(Component.select_version(
                             obj.active_version)(specs=obj)),
                         endpoint='component')]
        if isinstance(obj, ActivatedMaterial):
            return [dict(text="These activations of it are deleted:",
                         docs=list(CosmogenicActivation.select_version(
                             obj.active_version)(material=obj)),
                         endpoint='emissionspec')]
        if isinstance(obj, HitEfficiency):
            count = find_sourceterms(obj).count()
            return [dict(text=f"Removed from {count} source terms, which "
                              "will use any other matching hit efficiencies")]
        return []

    def get_mtime(self, objid):
        obj = self.queryset.only('version_tags', 'original_id', 'modified')\
            .get_or_404(objid)
        mtime = find_sourceterms(obj).scalar('modified').first()
        return mtime or obj.modified

    def _setup_processing(self):
        """ Create URL preprocessing rules """
        # before load_object, so a missing object isn't revealed first
        self.before_request(require_for_changes(
            Role.editor, form_endpoints={'edit', 'delete', 'import_'}))

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
            # specs belonging to one component are only listed on request
            if (self.clsname == 'emissionspec' and
                    not flask.request.args.get('owned')):
                data = data(owner=None)
            return flask.render_template(f'overview_{self.clsname}.html',
                                         cls=self.doc_cls, data=data)

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
            activations = []
            if self.clsname == 'activatedmaterial':
                activations = CosmogenicActivation.select_version(
                    flask.g.active_version)(material=flask.g.object)
            # types of spec a component can create for itself
            spectypes = [name.rsplit('.', 1)[-1]
                         for name in EmissionSpec._subclasses]
            return flask.render_template(f'view_{self.clsname}.html',
                                         components=components,
                                         activations=activations,
                                         spectypes=spectypes)

        @self.get('/diff/<itemid>/<other_version>')
        def diff(itemid, other_version):
            """ Compare an item in the active version with its copy in
            `other_version`. The item may be missing from either one
            """
            this = flask.g.active_version
            if not version_exists(other_version):
                flask.abort(404, f"Version '{other_version}' does not exist")
            try:
                comparison = compare_document(self.doc_cls, itemid, this,
                                              other_version)
            except InvalidId:
                flask.abort(404, f"No {self.doc_cls.__name__} with id "
                                 f"{itemid}")
            if comparison.left is None and comparison.right is None:
                flask.abort(404, f"No {self.doc_cls.__name__} with id "
                                 f"{itemid} in either version")
            refs = {}
            for side, tag in (('left', this), ('right', other_version)):
                ids = [ref for entry in comparison.entries
                       for ref in find_refs(getattr(entry, side))]
                refs[side] = describe_refs(ids, tag)
            return flask.render_template(
                'diff_document.html', comparison=comparison,
                other_version=other_version, refs=refs,
                format_value=format_raw_value)

        @self.post('/import_from/<from_version>/<itemid>')
        def import_from(from_version, itemid):
            """ Replace the active version's copy of an item with the copy
            in `from_version`
            """
            this = flask.g.active_version
            try:
                doc = import_document(self.doc_cls, itemid, from_version,
                                      this)
            except InvalidId:
                flask.abort(404, f"No {self.doc_cls.__name__} with id "
                                 f"{itemid}")
            except KeyError as e:
                flask.abort(404, f"Version {e} does not exist")
            except VersionControlError as e:
                missing = describe_refs(map(ObjectId, e.problems),
                                        from_version)
                flask.flash(flask.render_template_string(
                    "{{ message }}{% if names %}:<ul>{% for name in names %}"
                    "<li>{{ name }}</li>{% endfor %}</ul>{% endif %}",
                    message=str(e),
                    names=[missing.get(ObjectId(p), (None, p))[1]
                           for p in e.problems]), 'danger')
                return flask.redirect(flask.url_for(
                    '.diff', itemid=itemid, other_version=from_version))
            flask.flash(f"Imported {getattr(doc, 'name', None) or itemid} "
                        f"from '{from_version}'", 'success')
            return flask.redirect(flask.url_for('.view', object=doc))

        @self.route('/new', methods=['GET', 'POST'])
        @self.route('/<objid>/edit', methods=['GET', 'POST'])
        def edit():
            check_writable(flask.g.active_version)
            req = flask.request
            errors = {}
            owner = None
            if 'object' not in flask.g:
                flask.g.object = self.new_document(req.args.get('type'))
                # a new spec for one component, see Component.add_owned_spec
                if ownerid := req.args.get('owner'):
                    owner = get_or_404(Component.select_version(
                        flask.g.active_version), ownerid)
                    flask.g.object.owner = owner
            if req.method == 'POST':
                obj = flask.g.object
                # generated emission sources the user edits become overrides
                if overrides := hasattr(obj, 'mark_overrides'):
                    shown = list_snapshot(obj, 'sources', obj.OVERRIDE_FIELDS)
                obj = update_object(obj, req.form, errors=errors)
                if overrides:
                    obj.mark_overrides(edited_rows(shown, req.form, 'sources'))
                if not errors:
                    try:
                        if owner:
                            owner.add_owned_spec(obj)
                        else:
                            obj.save()
                    except me.ValidationError as e:
                        errors = validation_fields(e)
                    else:
                        flask.flash(f"Successfully saved {obj}", 'success')
                        if owner:
                            return flask.redirect(flask.url_for(
                                'component.view', object=owner))
                        return flask.redirect(flask.url_for('.view',
                                                            object=obj))
            return flask.render_template(f'edit_{self.clsname}.html',
                                         form=flask.request.form,
                                         errors=errors), \
                400 if errors else 200

        @self.route('/<objid>/delete', methods=['GET', 'POST'])
        def delete():
            check_writable(flask.g.active_version)
            obj = flask.g.object
            if flask.request.method == 'POST':
                # only removes it from the active version
                obj.delete()
                flask.flash(f"Deleted {obj} from {flask.g.active_version}",
                            'success')
                return flask.redirect(flask.url_for('.overview'))
            return flask.render_template('delete_document.html',
                                         impact=self.delete_impact(obj))

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

            def attachment_id(attachmentid) -> ObjectId:
                """ The id of one of the active object's attachments """
                if (not ObjectId.is_valid(attachmentid) or
                        ObjectId(attachmentid) not in
                        [a.id for a in flask.g.object.attachments]):
                    flask.abort(404)
                return ObjectId(attachmentid)

            @self.get('<objid>/attachments/<attachmentid>')
            def get_attachment(attachmentid):
                attachment = next(self.doc_cls.objects(id=flask.g.object.id).aggregate(
                    [{'$unwind': '$attachments'},
                     {'$replaceWith': '$attachments'},
                     {'$match': {'id': attachment_id(attachmentid)}},
                     ]), None)
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

            @self.post('<objid>/attachments/<attachmentid>/delete')
            def delete_attachment(attachmentid):
                check_writable(flask.g.active_version)
                obj = flask.g.object
                attachment = next(a for a in obj.attachments
                                  if a.id == attachment_id(attachmentid))
                # a versioned modify, so other versions keep it
                obj.modify(pull__attachments__id=attachment.id)
                flask.flash(f"Removed attachment {attachment.filename}",
                            'success')
                return flask.redirect(flask.url_for('.attachments',
                                                    object=obj))




        if 'spectra' in self.doc_cls._fields:
            self._create_spectra_endpoints()
        if issubclass(self.doc_cls, Component):
            self._create_plot_endpoints()

        if issubclass(self.doc_cls, Component):
            @self.post('/<objid>/makespecific/<specid>')
            def make_specific(specid):
                """ Replace a shared spec with a copy of our own """
                check_writable(flask.g.active_version)
                component = flask.g.object
                spec = next((s for s in component.specs
                             if str(getattr(s, 'original_id', '')) == specid),
                            None)
                if spec is None:
                    flask.abort(404, "No such spec on this component")
                if spec.owner is not None:
                    flask.abort(400, f"{spec.name} already belongs to "
                                     f"{component.name}")
                copy = component.make_specific(spec)
                flask.flash(f"{component.name} now has its own copy of "
                            f"{spec.name}", 'success')
                return flask.redirect(flask.url_for('emissionspec.edit',
                                                    object=copy))

        @self.get('/<objid>/sourceterms')
        def sourceterms():
            sourceterms = find_sourceterms(flask.g.object)
            return flask.render_template('view_sourceterms.html',
                                         sourceterms=sourceterms)

    def _create_plot_endpoints(self):
        """ Data for the spectra and budget plots of a Component """
        @self.get('/<objid>/spectra.json')
        def spectra_json():
            # only shown, so stored spectra don't need their expressions
            results = CalculatedResults.for_object(
                flask.g.object, flask.g.get('relativeto'),
                correlations=False)
            config = get_settings(flask.g.active_version)\
                .hiteffdbconfig.display_spectra
            spectra = {}
            for name, hist in (results.spectra if results else {}).items():
                cfg = config.get(name)
                if hist is None or (cfg and cfg.hide):
                    continue
                spectra[(cfg and cfg.display_name) or name] = \
                    histogram_json(hist, cfg and cfg.display_unit)
            return flask.jsonify(spectra)

        @self.get('/<objid>/spectrum.json')
        def spectrum_json():
            """ One spectrum's total for the `filters` in BudgetFilter JSON
            format, and optionally its largest parts by `groupby`
            """
            args = flask.request.args
            names = spectrum_names(flask.g.active_version)
            name = args.get('spectrum') or (names[0][0] if names else '')
            groupby = args.get('groupby') or None
            if names and name not in dict(names):
                return flask.jsonify(error=dict(
                    message=f"Unknown spectrum '{name}'")), 400
            config = get_settings(flask.g.active_version)\
                .hiteffdbconfig.display_spectra
            unit = name in config and config[name].display_unit or None
            relativeto = flask.g.get('relativeto')

            def hist_json(hist):
                return hist and histogram_json(hist, unit)

            def calculate():
                result = spectrum_breakdown(flask.g.object, name, filters,
                                            relativeto, groupby=groupby)
                return dict(
                    spectrum=name, groupby=groupby,
                    count=result['count'], ntotal=result['ntotal'],
                    total=hist_json(result['total']),
                    curves=[dict(key=list(c['key'])
                                 if isinstance(c['key'], tuple) else c['key'],
                                 label=c['label'], rank=c['rank'],
                                 value=hist_json(c['value']))
                            for c in result['curves']],
                    other=hist_json(result['other']))
            try:
                filters = BudgetFilter.from_json(args.get('filters'))
                # evaluating the sums is most of the work, so keep the JSON
                data = cached_filtered(flask.g.object, filters, relativeto,
                                       ('spectrum.json', name, groupby,
                                        unit and str(unit)), calculate)
            except (ValueError, PintError) as e:
                return flask.jsonify(error=dict(message=str(e))), 400
            return flask.jsonify(
                spectra=[dict(key=key, name=label) for key, label in names],
                **data)

        @self.get('/<objid>/dashboard.json')
        def dashboard_json():
            """ Every budget breakdown for the `filters` in BudgetFilter
            JSON format, and the totals with and without them
            """
            args = flask.request.args
            scalars = available_scalars(flask.g.active_version)
            scalar = args.get('scalar') or (scalars[0] if scalars else '')
            try:
                filters = BudgetFilter.from_json(args.get('filters'))
                result = dashboard(
                    flask.g.object, scalar, filters,
                    relativeto=flask.g.get('relativeto'),
                    unit=args.get('unit') or None)
            except (ValueError, PintError) as e:
                return flask.jsonify(error=dict(message=str(e))), 400

            def row_json(row):
                key = row['key']
                return dict(key=list(key) if isinstance(key, tuple) else key,
                            label=row['label'], depth=row['depth'],
                            selected=row['selected'], size=row['size'],
                            children=row.get('children', False),
                            value=scalar_json(row['value']))
            charts = {groupby: [row_json(row) for row in rows]
                      for groupby, rows in result['charts'].items()}

            def total_json(value):
                """ scalar_json plus its magnitude formatted as text and as
                LaTeX, as in the contributions table
                """
                data = scalar_json(value)
                if data is not None:
                    data.update(text='{:S}'.format(value.m),
                                latex='{:LS}'.format(value.m))
                return data
            return flask.jsonify(
                scalar=scalar, scalars=scalars,
                units=unit_str(result['units']), filters=filters.todict(),
                count=result['count'], ntotal=result['ntotal'],
                total=total_json(result['total']),
                unfiltered=total_json(result['unfiltered']),
                breadcrumb=[dict(key=list(path), label=label)
                            for path, label in result['breadcrumb']],
                charts=charts)

        @self.get('/<objid>/results_table')
        def results_table():
            """ The contributions table for the `filters` in BudgetFilter
            JSON format
            """
            try:
                filters = BudgetFilter.from_json(
                    flask.request.args.get('filters'))
                result = table(flask.g.object, filters,
                               relativeto=flask.g.get('relativeto'))
            except ValueError as e:
                return markupsafe.escape(str(e)), 400
            return flask.render_template(
                'results_table.html', table=result, unit_str=unit_str,
                filtered=bool(filters.entries or filters.root))

        @self.get('/<objid>/results')
        def results():
            return flask.render_template(
                'results_component.html', groupby=GROUPBY,
                scalars=available_scalars(flask.g.active_version),
                table=table(flask.g.object,
                            relativeto=flask.g.get('relativeto')),
                unit_str=unit_str)

    def _create_spectra_endpoints(self):
        """ Import, rename and delete the spectra of a HitEfficiency. Each
        redirects to the edit page with a flashed result
        """
        errors = (KeyError, ValueError, me.ValidationError, PintError)

        @self.get('/<objid>/spectra.json')
        @self.loads('spectra')
        def spectra_json():
            return flask.jsonify({
                name: histogram_json(hist)
                for name, hist in flask.g.object.spectra.items()
                if hist is not None})

        def done(message=None, error=None):
            if error is not None:
                message = error.args[0] if error.args else str(error)
                flask.flash(str(message), 'danger')
            else:
                flask.flash(message, 'success')
            return flask.redirect(flask.url_for('.edit', object=flask.g.object,
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
