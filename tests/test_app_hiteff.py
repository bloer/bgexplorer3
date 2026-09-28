""" Creating, editing and importing hit efficiencies and their spectra """
import importlib.resources
import io
import json
import re
import numpy as np
from werkzeug.datastructures import MultiDict
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.settings import get_settings, SpectrumROI
from bgexplorer.models.common import units
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase


class TestHitEffPages(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        config = get_settings('main')
        config.hiteffdbconfig.rois = [
            SpectrumROI(spectrum='s1', start=0*units.keV, stop=5*units.keV,
                        mode='integrate')]
        config.save()
        self.h1 = HitEfficiency(
            source='K40', location='c1', nprimaries=1000,
            scalars=dict(v1='0.1 +- 0.01 dru/mBq', v2='<0.5 dru/mBq'),
            spectra=dict(s1=Histogram(
                AsymmetricUncertainty(np.ones(10), np.zeros(10))
                * units('dru/mBq/keV'), np.arange(11) * units.keV))).save()
        vc.create_version('b', 'main')
        vc.create_tag('t', 'main')

    def get(self, version='b'):
        return HitEfficiency.select_version(version).get(source='K40')

    def edit_form(self, version='b'):
        """ The inputs of the edit page, as a form to submit """
        html = self.html(self.client.get(self.url('hitefficiency.edit',
                                                  version, object=self.h1)))
        html = re.sub(r'<template.*?</template>', '', html, flags=re.S)
        form = MultiDict()
        for tag in re.findall(r'<input[^>]*>', html):
            name = re.search(r'name="([^"]*)"', tag)
            value = re.search(r'value="([^"]*)"', tag)
            if name and 'type="checkbox"' not in tag:
                form.add(name.group(1), value.group(1) if value else '')
        return form

    def test_new(self):
        url = self.url('hitefficiency.edit', 'b')
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, data=MultiDict([
            ('source', 'U238'), ('location', 'c2'), ('norm', 'rate'),
            ('_listfields', 'scalars'),
            ('scalars.__key__', 'v1'), ('scalars.__value__', '1 dru/mBq')]))
        self.assertEqual(response.status_code, 302)
        h = HitEfficiency.select_version('b').get(source='U238')
        self.assertEqual(h.version_tags, ['b'])
        self.assertEqual(h.scalars_keys, ['v1'])
        self.assertEqual(HitEfficiency.select_version('main').count(), 1)

    def test_edit_keeps_spectra(self):
        form = self.edit_form()
        self.assertEqual(form.getlist('scalars.__key__'), ['v1', 'v2'])
        form.setlist('scalars.__key__', ['v1', 'renamed', 'v3'])
        form.setlist('scalars.__value__',
                     form.getlist('scalars.__value__') + ['2 dru/mBq'])
        form['location'] = 'c1b'
        response = self.client.post(self.url('hitefficiency.edit', 'b',
                                             object=self.h1), data=form)
        self.assertEqual(response.status_code, 302, self.html(response))
        h = self.get()
        self.assertEqual(h.location, 'c1b')
        self.assertEqual(list(h.scalars), ['v1', 'renamed', 'v3'])
        orig = self.get('main')
        # unchanged values round trip through the form
        for old, new in (('v1', 'v1'), ('v2', 'renamed')):
            self.assertEqual(str(h.scalars[new]), str(orig.scalars[old]))
        self.assertEqual(h.nprimaries, 1000)
        self.assertEqual(list(h.spectra), ['s1'])
        self.assertEqual(h.spectra_keys, ['s1'])
        self.assertEqual(str(h.rois), str(orig.rois))
        self.assertEqual(orig.location, 'c1')

    def test_edit_errors(self):
        form = self.edit_form()
        form.setlist('scalars.__key__', ['v1', 'v1'])
        response = self.client.post(self.url('hitefficiency.edit', 'b',
                                             object=self.h1), data=form)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Duplicate name', self.html(response))

    def test_import(self):
        path = (importlib.resources.files('bgexplorer.application.examples')
                .joinpath('qis_hiteffs.tar.gz'))
        vc.create_version('empty')
        response = self.client.post(
            self.url('hitefficiency.import_', 'empty'),
            data={'file': (io.BytesIO(path.read_bytes()), 'qis.tar.gz')},
            content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        count = HitEfficiency.select_version('empty').count()
        self.assertGreater(count, 0)
        self.assertIn(f'Imported {count} documents', self.html(response))
        h = HitEfficiency.select_version('empty').first()
        self.assertTrue(h.spectra_keys)

    def post_spectrum(self, endpoint, version='b', follow=True, **data):
        return self.client.post(self.url(f'hitefficiency.{endpoint}', version,
                                         object=self.h1),
                                data=data, content_type='multipart/form-data',
                                follow_redirects=follow)

    def test_spectra(self):
        html = self.html(self.client.get(self.url('hitefficiency.view', 'b',
                                                  object=self.h1)))
        self.assertIn('id="importspectrum"', html)
        csv = b"low,high,value\n0,1,2\n1,2,2\n2,10,2\n"
        response = self.post_spectrum(
            'import_spectrum', file=(io.BytesIO(csv), 'mine.csv'),
            units='dru/mBq/keV', binsunit='keV')
        self.assertIn("Imported spectrum 'mine'", self.html(response))
        self.assertEqual(sorted(self.get().spectra_keys), ['mine', 's1'])
        js = json.dumps(dict(value=[3, 3], bins=[0, 5, 10])).encode()
        response = self.post_spectrum(
            'import_spectrum', file=(io.BytesIO(js), 'x.json'), name='s1',
            units='dru/mBq/keV', binsunit='keV', overwrite='on')
        self.assertIn("Imported spectrum 's1'", self.html(response))
        (roi,) = self.get().rois.values()
        self.assertAlmostEqual(roi.m.nominal_value, 15)

        response = self.post_spectrum('import_spectrum',
                                      file=(io.BytesIO(b"0 1 2\n3 4 5\n"),
                                            'bad.txt'))
        self.assertIn('contiguous', self.html(response))
        response = self.post_spectrum('import_spectrum',
                                      file=(io.BytesIO(csv), 'mine.csv'))
        self.assertIn('already exists', self.html(response))

        response = self.post_spectrum('rename_spectrum', name='mine',
                                      newname='yours')
        self.assertIn("Renamed spectrum 'mine' to 'yours'",
                      self.html(response))
        response = self.post_spectrum('delete_spectrum', name='yours')
        self.assertIn("Deleted spectrum 'yours'", self.html(response))
        response = self.post_spectrum('delete_spectrum', name='yours')
        self.assertIn("No spectrum 'yours'", self.html(response))
        self.assertEqual(self.get().spectra_keys, ['s1'])
        self.assertEqual(self.get('main').spectra_keys, ['s1'])

    def test_readonly(self):
        html = self.html(self.client.get(self.url('hitefficiency.overview',
                                                  't')))
        self.assertNotIn('id="newhiteff"', html)
        html = self.html(self.client.get(self.url('hitefficiency.view', 't',
                                                  object=self.h1)))
        self.assertNotIn('id="importspectrum"', html)
        self.assertNotIn('deletespectrum', html)
        self.assertEqual(self.client.post(self.url('hitefficiency.edit', 't',
                                                   object=self.h1))
                         .status_code, 403)
        for endpoint in ('import_spectrum', 'rename_spectrum',
                         'delete_spectrum'):
            with self.subTest(endpoint=endpoint):
                response = self.post_spectrum(endpoint, 't', follow=False,
                                              name='s1', newname='x')
                self.assertEqual(response.status_code, 403)
        html = self.html(self.client.get(self.url('hitefficiency.overview',
                                                  'b')))
        self.assertIn('id="newhiteff"', html)
