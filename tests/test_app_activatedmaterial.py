""" Creating and editing activated materials through the web interface """
from io import BytesIO
from werkzeug.datastructures import MultiDict
from bgexplorer.models.component import Component
from bgexplorer.models.cosmogenic import ActivatedMaterial
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase
from tests.test_app_emissionspec import mainform, rows, set_rows
from tests.test_cosmogenic import copper


class TestActivatedMaterialPages(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.cu = copper(publication=dict(shortlabel='Cu ref')).save()
        vc.create_version('b', 'main')

    def get(self, name='Cu', version='b'):
        return ActivatedMaterial.select_version(version).get(name=name)

    def test_overview_and_view(self):
        html = self.html(self.client.get(
            self.url('activatedmaterial.overview', 'b')))
        self.assertIn('id="newactivatedmaterial"', html)
        self.assertIn('Co60, Mn54', html)
        self.assertIn('Cu ref', html)
        self.assertIn(self.url('activatedmaterial.overview', 'b'),
                      self.html(self.client.get(self.url('overview', 'b'))))
        response = self.client.get(self.url('activatedmaterial.view', 'b',
                                            object=self.cu))
        self.assertEqual(response.status_code, 200)
        html = self.html(response)
        self.assertIn('Mn54', html)
        self.assertIn('id="otherversions"', html)

    def test_new(self):
        url = self.url('activatedmaterial.edit', 'b')
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, data=MultiDict([
            ('name', 'Ge'), ('material', 'germanium'),
            ('_listfields', 'isotopes'),
            ('isotopes.id', ''), ('isotopes.isotope', 'Ge68'),
            ('isotopes.activationrate', '41 +- 4 1/kg/day'),
            ('isotopes.comment', ''),
            ('isotopes.id', ''), ('isotopes.isotope', 'H3'),
            ('isotopes.activationrate', '27 1/kg/day'),
            ('isotopes.comment', 'tritium')]))
        self.assertEqual(response.status_code, 302)
        ge = self.get('Ge')
        self.assertEqual(ge.version_tags, ['b'])
        self.assertEqual([i.isotope for i in ge.isotopes], ['Ge68', 'H3'])
        self.assertEqual(ge.isotopes[1].comment, 'tritium')
        self.assertEqual(ActivatedMaterial.select_version('main').count(), 1)

    def test_edit(self):
        url = self.url('activatedmaterial.edit', 'b', object=self.cu)
        form = mainform(self.html(self.client.get(url)))
        isotopes = rows(form, 'isotopes')
        self.assertEqual([i['isotope'] for i in isotopes], ['Co60', 'Mn54'])
        isotopes[0]['activationrate'] = '50 +- 5 1/kg/day'
        set_rows(form, isotopes[:1], 'isotopes')
        self.assertEqual(self.client.post(url, data=form).status_code, 302)
        cu = self.get()
        self.assertEqual([i.isotope for i in cu.isotopes], ['Co60'])
        self.assertEqual(cu.isotopes[0].id, self.cu.isotopes[0].id)
        self.assertAlmostEqual(cu.isotopes[0].activationrate.m.mode, 50)
        # other versions are unchanged
        self.assertEqual(len(self.get(version='main').isotopes), 2)

    def test_errors(self):
        url = self.url('activatedmaterial.edit', 'b', object=self.cu)
        form = mainform(self.html(self.client.get(url)))
        isotopes = rows(form, 'isotopes')
        isotopes[1]['isotope'] = '60Co'
        set_rows(form, isotopes, 'isotopes')
        response = self.client.post(url, data=form)
        self.assertEqual(response.status_code, 400)
        self.assertIn('listed more than once', self.html(response))

        isotopes[1]['isotope'] = 'Fe56'
        set_rows(form, isotopes, 'isotopes')
        response = self.client.post(url, data=form)
        self.assertEqual(response.status_code, 400)
        self.assertIn('not a radioactive isotope', self.html(response))
        self.assertEqual(self.get().isotopes[1].isotope, 'Mn54')

    def test_import_export(self):
        exported = self.client.get(self.url('activatedmaterial.get_json', 'b',
                                            object=self.cu)).get_data()
        response = self.client.post(
            self.url('activatedmaterial.import_', 'b'),
            data={'file': (BytesIO(exported), 'cu.json')})
        self.assertEqual(response.status_code, 302)
        copies = ActivatedMaterial.select_version('b')(name='Cu')
        self.assertEqual(copies.count(), 2)
        self.assertEqual({len(c.isotopes) for c in copies}, {2})


class TestComponentActivatedMaterial(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.cu = copper().save()
        self.c1 = Component(name='c1', mass='1 kg',
                            activated_material=self.cu).save()
        self.c2 = Component(name='c2', mass='1 kg').save()
        vc.create_version('b', 'main')

    def get(self, name, version='b'):
        return Component.select_version(version).get(name=name)

    def edit(self, component, value, version='b'):
        url = self.url('component.edit', version, object=component)
        form = mainform(self.html(self.client.get(url)))
        self.assertIn('activated_material', form)
        form['activated_material'] = value
        return self.client.post(url, data=form)

    def test_choose_and_clear(self):
        html = self.html(self.client.get(
            self.url('component.edit', 'b', object=self.c2)))
        self.assertIn('Cosmogenic activation', html)
        self.assertIn(self.url('activatedmaterial.overview', 'b',
                               embedded=1), html)
        response = self.edit(self.c2, str(self.cu.original_id))
        self.assertEqual(response.status_code, 302)
        c2 = self.get('c2')
        self.assertEqual(c2.activated_material.name, 'Cu')
        self.assertIsNone(self.get('c2', 'main').activated_material)
        html = self.html(self.client.get(
            self.url('component.view', 'b', object=c2)))
        self.assertIn('id="activated_material"', html)
        self.assertIn(self.url('activatedmaterial.view', 'b',
                               object=self.cu), html)

        # the form shows the current value, and submits it unchanged
        self.assertEqual(self.edit(self.c1, str(self.cu.original_id))
                         .status_code, 302)
        self.assertEqual(self.get('c1').activated_material.name, 'Cu')
        self.assertEqual(self.edit(self.c1, '').status_code, 302)
        self.assertIsNone(self.get('c1').activated_material)
        self.assertEqual(self.get('c1', 'main').activated_material.name,
                         'Cu')

    def test_embedded_overview(self):
        html = self.html(self.client.get(
            self.url('activatedmaterial.overview', 'b', embedded=1)))
        self.assertNotIn('<nav', html)
        self.assertIn('class="table', html)
        self.assertIn(f'data-original_id="{self.cu.original_id}"', html)

    def test_delete(self):
        html = self.html(self.client.get(
            self.url('activatedmaterial.delete', 'b', object=self.cu)))
        self.assertIn('Removed from these components', html)
        self.assertIn(self.url('component.view', 'b', object=self.c1), html)
        response = self.client.post(
            self.url('activatedmaterial.delete', 'b', object=self.cu))
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.get('c1').activated_material)
        self.assertEqual(self.get('c1', 'main').activated_material.name,
                         'Cu')
