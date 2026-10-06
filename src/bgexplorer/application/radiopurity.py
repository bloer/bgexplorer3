""" Search radiopurity.org and import the results as Assays """
import flask
from mongoengine.errors import ValidationError
from ..models.assay import Assay
from ..models import radiopurity as rp
from ..models.verdoc import check_writable
from .auth import require_for_changes, Role


def create_radiopurity_blueprint() -> flask.Blueprint:
    bp = flask.Blueprint('radiopurity', __name__)
    # searching is only useful for importing, so needs editor too
    bp.before_request(require_for_changes(Role.editor,
                                          form_endpoints={'search'}))

    def get_query():
        args = flask.request.values
        term = args.get('q', '').strip()
        # an unchecked box isn't sent, so a new search form includes synonyms
        synonyms = bool(args.get('include_synonyms')) or 'q' not in args
        return term, synonyms

    def imported(records):
        """ Assays in this version already imported from `records` """
        ids = [r.id for r in records if r.id]
        found = Assay.select_version(flask.g.active_version)(
            radiopurityid__in=ids).only('original_id', 'name',
                                        'radiopurityid')
        return {a.radiopurityid: a for a in found}

    @bp.get('/radiopurity')
    def search():
        term, synonyms = get_query()
        records, existing = [], {}
        if term:
            try:
                records, warnings = rp.search(term, synonyms)
            except rp.RadiopurityError as e:
                flask.flash(str(e), 'danger')
            else:
                for warning in warnings:
                    flask.flash(warning, 'warning')
                existing = imported(records)
        return flask.render_template('radiopurity_search.html', term=term,
                                     include_synonyms=synonyms,
                                     records=records, existing=existing)

    @bp.post('/radiopurity')
    def import_selected():
        check_writable(flask.g.active_version)
        term, synonyms = get_query()
        selected = flask.request.form.getlist('id')
        back = flask.url_for('.search', q=term,
                             include_synonyms=synonyms or None)
        if not selected:
            flask.flash("Nothing selected to import", 'warning')
            return flask.redirect(back)
        # search again rather than trusting values sent back by the client
        try:
            records, _ = rp.search(term, synonyms)
        except rp.RadiopurityError as e:
            flask.flash(str(e), 'danger')
            return flask.redirect(back)

        byid = {r.id: r for r in records}
        created, errors = [], []
        for recid in selected:
            if (record := byid.get(recid)) is None:
                errors.append((recid, "no longer in the search results"))
                continue
            assay = record.to_assay(flask.g.active_version)
            try:
                assay.save()
            except ValidationError as e:
                errors.append((record.name, str(e)))
            else:
                created.append(assay)

        if created:
            flask.flash(flask.render_template_string(
                "Imported {{ created | length }} assays:<ul>"
                "{% for a in created %}<li><a href=\""
                "{{ url_for('emissionspec.view', object=a) }}\">{{ a.name }}"
                "</a></li>{% endfor %}</ul>", created=created), 'success')
        if errors:
            flask.flash(flask.render_template_string(
                "Not imported:<ul>{% for label, error in errors %}"
                "<li>{{ label }}: {{ error }}</li>{% endfor %}</ul>",
                errors=errors), 'danger')
        return flask.redirect(flask.url_for('emissionspec.overview')
                              if created else back)

    return bp
