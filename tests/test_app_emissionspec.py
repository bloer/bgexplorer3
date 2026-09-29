""" Creating and editing emission specs through the web interface """
import re
from io import BytesIO
from werkzeug.datastructures import MultiDict
from bgexplorer.models.assay import Assay
from bgexplorer.models.component import Component
from bgexplorer.models.emissionspec import (EmissionSpec, EmissionSource,
                                            Multiplier, SourceCategory)
from bgexplorer.models.fields import get_fromstr
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.sourceterm import CalculatedResults
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase


def mainform(html):
    """ The inputs and selects of #mainform, as a form to submit unchanged """
    html = re.sub(r'<template.*?</template>', '', html, flags=re.S)
    html = re.search(r'<form method="POST" id="mainform">(.*?)</form>',
                     html, flags=re.S).group(1)
    form = MultiDict()
    for tag in re.finditer(r'<input[^>]*>|<select[^>]*>.*?</select>', html,
                           flags=re.S):
        tag = tag.group(0)
        name = re.search(r'name="([^"]*)"', tag)
        if not name or 'type="checkbox"' in tag:
            continue
        if tag.startswith('<select'):
            # the selected option, else the first like a browser
            options = re.findall(r'<option value="([^"]*)"( selected)?', tag)
            value = next((v for v, sel in options if sel), options[0][0])
        else:
            value = re.search(r'value="([^"]*)"', tag)
            value = value.group(1) if value else ''
        form.add(name.group(1), value)
    return form


def rows(form, fieldname='sources'):
    """ The rows of a dynamictable in `form` as a list of dicts """
    prefix = fieldname + '.'
    columns = {k[len(prefix):]: form.getlist(k) for k in form
               if k.startswith(prefix)}
    return [{k: v[i] for k, v in columns.items()}
            for i in range(len(columns['id']))]


def set_rows(form, newrows, fieldname='sources'):
    """ Replace the rows of a dynamictable in `form` """
    for key in [k for k in form if k.startswith(fieldname + '.')]:
        del form[key]
    for row in newrows:
        for k, v in row.items():
            form.add(f'{fieldname}.{k}', v)


class TestEmissionSpecPages(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        HitEfficiency(source='Th232', location='c1',
                      scalars=dict(v1='0.1 +- 0.01 dru/mBq')).save()
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='Th232', rate='10 +- 1 mBq/kg'),
            EmissionSource(name='K40', rate='<25 mBq/kg')]).save()
        self.e2 = EmissionSpec(name='e2', sources=[
            EmissionSource(name='U238', rate='10 +- 1 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='2 kg', location='c1',
                            specs=[self.e1]).save()
        vc.create_version('b', 'main')
        vc.create_tag('t', 'main')

    def get(self, name='e1', version='b'):
        return EmissionSpec.select_version(version).get(name=name)

    def edit_form(self, spec=None, version='b'):
        spec = spec or self.e1
        return mainform(self.html(self.client.get(
            self.url('emissionspec.edit', version, object=spec))))

    def post(self, form, spec=None, version='b'):
        spec = spec or self.e1
        return self.client.post(self.url('emissionspec.edit', version,
                                         object=spec), data=form)

    def total(self, version='b'):
        c1 = Component.select_version(version).get(name='c1')
        results = CalculatedResults.for_object(c1, save=False, spectra=False,
                                               cache=False)
        return str(results.scalars['v1'])

    def test_new(self):
        url = self.url('emissionspec.edit', 'b')
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, data=MultiDict([
            ('name', 'new'), ('_listfields', 'sources'),
            ('sources.id', ''), ('sources.name', 'K40'),
            ('sources.rate', '5 +- 1 mBq/kg'),
            ('sources.id', ''), ('sources.name', 'Co60'),
            ('sources.rate', '2 mBq/m**2')]))
        self.assertEqual(response.status_code, 302)
        spec = self.get('new')
        self.assertIn(self.url('emissionspec.view', 'b', object=spec),
                      response.headers['Location'])
        self.assertIs(type(spec), EmissionSpec)
        self.assertEqual(spec.version_tags, ['b'])
        self.assertEqual([s.name for s in spec.sources], ['K40', 'Co60'])
        self.assertIsNotNone(spec.sources[0].id)
        self.assertNotEqual(spec.sources[0].id, spec.sources[1].id)
        self.assertEqual(spec.sources[1].multiplier, Multiplier.surface)
        self.assertEqual(EmissionSpec.select_version('main').count(), 2)

    def test_new_assay(self):
        html = self.html(self.client.get(self.url('emissionspec.overview',
                                                  'b')))
        self.assertIn('id="newemissionspec"', html)
        self.assertIn('id="newassay"', html)
        url = self.url('emissionspec.edit', 'b', type='assay')
        html = self.html(self.client.get(url))
        self.assertIn('New Assay', html)
        self.assertIn('name="sample.material"', html)
        self.assertIn('Sample Information', html)
        self.assertIn('Citation', html)
        self.assertIn('name="extra_metadata.__key__"', html)
        # lists within embedded documents aren't in the form
        self.assertNotIn('measurement.results', html)
        response = self.client.post(url, data=MultiDict([
            ('name', 'assay1'), ('sample.material', 'copper'),
            ('sample.mass', '2 kg'), ('publication.shortlabel', 'ref'),
            ('extra_metadata.__key__', 'lot'),
            ('extra_metadata.__value__', '7'),
            ('sources.id', ''), ('sources.name', 'K40'),
            ('sources.rate', '5 +- 1 mBq/kg')]))
        self.assertEqual(response.status_code, 302)
        assay = self.get('assay1')
        self.assertIs(type(assay), Assay)
        self.assertEqual(assay.sample.material, 'copper')
        self.assertEqual(assay.publication.shortlabel, 'ref')
        self.assertEqual(assay.extra_metadata, {'lot': '7'})
        self.assertEqual(assay.category, SourceCategory.assay)
        self.assertEqual(assay.sources[0].category, SourceCategory.assay)
        # and it can be edited
        form = mainform(self.html(self.client.get(
            self.url('emissionspec.edit', 'b', object=assay))))
        self.assertEqual(form['sample.material'], 'copper')
        form['sample.vendor'] = 'acme'
        self.assertEqual(self.post(form, assay).status_code, 302)
        assay = self.get('assay1')
        self.assertEqual(assay.sample.vendor, 'acme')
        self.assertEqual(assay.sample.material, 'copper')
        self.assertEqual(
            self.client.get(self.url('emissionspec.edit', 'b', type='nope'))
            .status_code, 400)

    def test_edit_sources(self):
        before = self.total()
        form = self.edit_form()
        self.assertEqual(self.post(form).status_code, 302)
        e1 = self.get()
        self.assertEqual([(s.id, s.name, get_fromstr(s.rate), s.multiplier)
                          for s in e1.sources],
                         [(s.id, s.name, get_fromstr(s.rate), s.multiplier)
                          for s in self.e1.sources])
        self.assertEqual(self.total(), before)

        th232, k40 = rows(form)
        self.assertEqual(th232['multiplier'], 'mass')
        th232['rate'] = '20 +- 1 mBq/kg'
        new = dict(th232, id='', name='Rn222', rate='3 mBq/m**2',
                   category='radon', multiplier='surface_area')
        set_rows(form, [th232, new])
        self.assertEqual(self.post(form).status_code, 302)
        e1 = self.get()
        self.assertEqual([s.name for s in e1.sources], ['Th232', 'Rn222'])
        self.assertEqual(get_fromstr(e1.sources[0].rate), '20 +- 1 mBq/kg')
        self.assertEqual(e1.sources[0].id, self.e1.sources[0].id)
        self.assertEqual(e1.sources[1].category, SourceCategory.radon)
        self.assertEqual(e1.sources[1].multiplier, Multiplier.surface)
        # the component's results follow
        self.assertNotEqual(self.total(), before)
        # main is untouched
        self.assertEqual(len(self.get(version='main').sources), 2)
        self.assertEqual(self.total('main'), before)

    def test_edit_errors(self):
        form = self.edit_form()
        form.setlist('sources.rate', ['10 kg', '<25 mBq/kg'])
        response = self.post(form)
        self.assertEqual(response.status_code, 400)
        html = self.html(response)
        self.assertIn('id="toperror"', html)
        self.assertIn('sources.0', html)
        # the error is shown in the table cell
        cell = re.search(r'<input[^>]*name="sources.rate"[^>]*>', html)
        self.assertIn('is-invalid', cell.group(0))
        self.assertEqual(get_fromstr(self.get().sources[0].rate),
                         '10 +- 1 mBq/kg')

    def test_generated_sources(self):
        """ generated sources are defaults, which the user can override """
        html = self.html(self.client.get(
            self.url('emissionspec.edit', 'b', object=self.e2)))
        self.assertEqual(html.count('class="badge text-bg-info me-1 '
                                    'generated"'), 2)
        self.assertIn('default (from U238)', html)
        form = mainform(html)
        self.assertEqual([r['name'] for r in rows(form)],
                         ['U238', 'U235', 'Ra226'])
        # unchanged, they stay generated
        self.assertEqual(self.post(form, self.e2).status_code, 302)
        e2 = self.get('e2')
        self.assertEqual([(s.name, bool(s.generated_from))
                          for s in e2.sources],
                         [('U238', False), ('U235', True), ('Ra226', True)])

        # an edited default is an override, and doesn't follow U238
        u238, u235, ra226 = rows(form)
        ra226['rate'] = '4 +- 1 mBq/kg'
        u238['rate'] = '20 +- 1 mBq/kg'
        set_rows(form, [u238, u235, ra226])
        self.assertEqual(self.post(form, self.e2).status_code, 302)
        e2 = self.get('e2')
        self.assertEqual([s.name for s in e2.sources],
                         ['U238', 'U235', 'Ra226'])
        self.assertIsNone(e2.sourcemap['Ra226'].generated_from)
        self.assertEqual(get_fromstr(e2.sourcemap['Ra226'].rate),
                         '4 +- 1 mBq/kg')
        # U235 is still a default, so it follows
        self.assertIsNotNone(e2.sourcemap['U235'].generated_from)
        self.assertAlmostEqual(
            e2.sourcemap['U235'].rate.to('mBq/kg').m.mode,
            2 * self.e2.sourcemap['U235'].rate.to('mBq/kg').m.mode)
        # a comment doesn't make an override
        form = mainform(self.html(self.client.get(
            self.url('emissionspec.edit', 'b', object=self.e2))))
        self.assertEqual(html.count('generated"'), 2)
        u238, u235, ra226 = rows(form)
        u235['comment'] = 'note'
        set_rows(form, [u238, u235, ra226])
        self.assertEqual(self.post(form, self.e2).status_code, 302)
        self.assertIsNotNone(self.get('e2').sourcemap['U235'].generated_from)

        # deleting the override restores the default
        set_rows(form, [u238, u235])
        self.assertEqual(self.post(form, self.e2).status_code, 302)
        e2 = self.get('e2')
        self.assertEqual([s.name for s in e2.sources],
                         ['U238', 'U235', 'Ra226'])
        self.assertIsNotNone(e2.sourcemap['Ra226'].generated_from)
        self.assertAlmostEqual(
            e2.sourcemap['Ra226'].rate.to('mBq/kg').m.mode, 20)

    def test_clone(self):
        html = self.html(self.client.get(self.url('emissionspec.view', 'b',
                                                  object=self.e1)))
        self.assertIn('id="cloneform"', html)
        response = self.client.post(self.url('emissionspec.clone', 'b',
                                             object=self.e1))
        self.assertEqual(response.status_code, 302)
        copy = self.get('e1 (copy)')
        self.assertEqual([s.name for s in copy.sources], ['Th232', 'K40'])

    def attach(self, spec, version='b'):
        return self.client.post(
            self.url('emissionspec.add_attachments', version, object=spec),
            data=dict(fupload=(BytesIO(b'hello'), 'hello.txt'),
                      description='assay report'))

    def test_attachments(self):
        view = self.html(self.client.get(self.url('emissionspec.view', 'b',
                                                  object=self.e1)))
        self.assertIn(self.url('emissionspec.attachments', 'b',
                               object=self.e1), view)
        response = self.attach(self.e1)
        self.assertEqual(response.status_code, 302)
        e1 = self.get()
        self.assertEqual([a.filename for a in e1.attachments], ['hello.txt'])
        # only on the branch
        self.assertEqual(self.get(version='main').attachments, [])
        html = self.html(self.client.get(self.url('emissionspec.attachments',
                                                  'b', object=e1)))
        self.assertIn('assay report', html)
        self.assertIn('id="uploadform"', html)
        attachment = self.url('emissionspec.get_attachment', 'b', object=e1,
                              attachmentid=e1.attachments[0].id)
        self.assertEqual(self.client.get(attachment).data, b'hello')
        # editing and cloning keep the data
        self.assertEqual(self.post(self.edit_form()).status_code, 302)
        self.assertEqual(self.get().attachments[0].data, b'hello')
        self.client.post(self.url('emissionspec.clone', 'b', object=e1))
        self.assertEqual(self.get('e1 (copy)').attachments[0].data, b'hello')
        # and assays have them too
        assay = Assay(name='a1').save()
        self.assertEqual(self.attach(assay, 'main').status_code, 302)
        self.assertEqual(len(Assay.select_version('main').get().attachments),
                         1)

    def test_readonly(self):
        """ tags have no buttons and refuse changes """
        html = self.html(self.client.get(self.url('emissionspec.overview',
                                                  't')))
        self.assertNotIn('id="newemissionspec"', html)
        html = self.html(self.client.get(self.url('emissionspec.view', 't',
                                                  object=self.e1)))
        self.assertNotIn('id="cloneform"', html)
        self.assertNotIn('>Edit</a>', html)
        for kwargs in ({}, {'object': self.e1}):
            with self.subTest(**kwargs):
                url = self.url('emissionspec.edit', 't', **kwargs)
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, data={'name': 'x'})
                                 .status_code, 403)
        self.assertEqual(self.attach(self.e1, 't').status_code, 403)
        self.assertNotIn('id="uploadform"', self.html(self.client.get(
            self.url('emissionspec.attachments', 't', object=self.e1))))
        e1 = self.get(version='t')
        self.assertEqual(e1.name, 'e1')
        self.assertEqual(e1.attachments, [])
