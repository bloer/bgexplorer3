""" Creating, cloning and importing components through the web interface """
import io
import json
import tarfile
import unittest
import flask
import mongoengine
from html import unescape
from bgexplorer.application.app import create_app
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.versioning import control as vc
from bgexplorer.models.settings import VersionSettings
from bgexplorer.versioning.record import VersionRecord
from bgexplorer.models.sourceterm import SourceTerm
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.cosmogenic import ActivatedMaterial
from tests.dbutil import TEST_MONGODB_URI
from tests.test_app import reset_database


class AppTestCase(unittest.TestCase):
    """ Base: a fresh app and database for each test class """
    # everyone can do everything, unless a subclass tests logins
    LOGIN_DISABLED = True

    @classmethod
    def setUpClass(cls):
        reset_database()
        cls.app = create_app(config={'MONGODB_URI': TEST_MONGODB_URI,
                                     'TESTING': True,
                                     'LOGIN_DISABLED': cls.LOGIN_DISABLED,
                                     'WTF_CSRF_ENABLED': False})

    @classmethod
    def tearDownClass(cls):
        mongoengine.disconnect()

    def setUp(self):
        for cls in (VersionRecord, VersionSettings, Component, EmissionSpec, SourceTerm,
                    HitEfficiency, ActivatedMaterial):
            cls.drop_collection()
        self.client = self.app.test_client()

    def url(self, endpoint, version='main', **values):
        with self.app.test_request_context():
            flask.g.active_version = version
            return flask.url_for(endpoint, **values)

    def html(self, response):
        return unescape(response.get_data(as_text=True))


class TestComponentPages(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='K40', rate='1 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='1 kg', specs=[self.e1]).save()
        self.a1 = Assembly(name='a1', children=[
            Placement(component=self.c1, weight=2)]).save()
        vc.create_version('b', 'main')
        vc.create_tag('t', 'main')

    def test_new(self):
        """ new documents are saved in the active version """
        for type_, cls in ((None, Component), ('assembly', Assembly)):
            with self.subTest(type=type_):
                url = self.url('component.edit', 'b', type=type_)
                self.assertEqual(self.client.get(url).status_code, 200)
                response = self.client.post(url, data={
                    'name': f'new {type_}', 'mass': '3 kg'})
                self.assertEqual(response.status_code, 302)
                obj = cls.select_version('b').get(name=f'new {type_}')
                self.assertIs(type(obj), cls)
                self.assertEqual(obj.version_tags, ['b'])
                self.assertIn(self.url('component.view', 'b', object=obj),
                              response.headers['Location'])
        self.assertEqual(Component.select_version('main').count(), 2)
        self.assertEqual(
            self.client.get(self.url('component.edit', 'b', type='nope'))
            .status_code, 400)

    def test_new_errors(self):
        url = self.url('component.edit', 'b')
        response = self.client.post(url, data={'mass': 'heavy'})
        self.assertEqual(response.status_code, 400)
        html = self.html(response)
        self.assertIn('id="toperror"', html)
        self.assertIn('mass', html)
        self.assertEqual(Component.select_version('b').count(), 2)

    def test_edit_redirects(self):
        url = self.url('component.edit', 'b', object=self.c1)
        response = self.client.post(url, data={'description': 'edited'})
        self.assertEqual(response.status_code, 302)
        c1 = Component.select_version('b').get(name='c1')
        self.assertEqual(c1.description, 'edited')
        self.assertEqual(c1.specs[0].name, 'e1')
        self.assertIsNone(Component.select_version('main')
                          .get(name='c1').description)

    def test_confidence_rating(self):
        """ the rating is set from the component form, not the assembly's """
        url = self.url('component.edit', 'b', object=self.c1)
        self.assertIn('name="confidence_rating"',
                      self.html(self.client.get(url)))
        self.assertNotIn('name="confidence_rating"', self.html(
            self.client.get(self.url('component.edit', 'b',
                                     object=self.a1))))
        self.assertNotIn('name="confidence_rating"', self.html(
            self.client.get(self.url('component.edit', 'b',
                                     type='assembly'))))
        for submitted, expected in (('3', 3), ('', None)):
            with self.subTest(submitted=submitted):
                response = self.client.post(
                    url, data={'confidence_rating': submitted})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(Component.select_version('b').get(
                    name='c1').confidence_rating, expected)
        self.client.post(url, data={'confidence_rating': '3'})
        html = self.html(self.client.get(url))
        self.assertIn('<option value="3" selected>', html)
        self.assertIn('<option value="">--</option>', html)
        response = self.client.post(url, data={'confidence_rating': '6'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('confidence_rating', self.html(response))

        # the document JSON has it only when rated
        c1 = Component.select_version('b').get(name='c1')
        apiurl = self.url('component.get_json', 'b', object=c1)
        self.client.post(url, data={'confidence_rating': '5'})
        data = json.loads(self.client.get(apiurl).get_data())
        self.assertEqual(data['confidence_rating'], 5)
        self.client.post(url, data={'confidence_rating': ''})
        data = json.loads(self.client.get(apiurl).get_data())
        self.assertNotIn('confidence_rating', data)

        self.assertIn('Confidence rating', self.html(self.client.get(
            self.url('component.view', 'b', object=self.c1))))
        self.assertNotIn('Confidence rating', self.html(self.client.get(
            self.url('component.view', 'b', object=self.a1))))

    def test_clone(self):
        html = self.html(self.client.get(self.url('component.view', 'b',
                                                  object=self.a1)))
        self.assertIn('id="cloneform"', html)
        response = self.client.post(self.url('component.clone', 'b',
                                             object=self.a1))
        self.assertEqual(response.status_code, 302)
        copy = Assembly.select_version('b').get(name='a1 (copy)')
        self.assertIn(self.url('component.edit', 'b', object=copy),
                      response.headers['Location'])
        self.assertEqual(copy.children[0].component.original_id,
                         self.c1.original_id)
        self.assertEqual(Assembly.select_version('main').count(), 1)

    def test_sourceterm_data(self):
        """ the table is paged, searched and sorted by the server, with no
        row missed or repeated
        """
        spec = EmissionSpec(name='e3', sources=[
            EmissionSource(name=f'S{i}', rate='1 mBq/kg')
            for i in range(5)]).save()
        c3 = Component(name='c3', mass='1 kg', specs=[spec]).save()
        url = self.url('component.sourceterm_data', object=c3)

        def data(**query):
            response = self.client.get(url, query_string=query)
            self.assertEqual(response.status_code, 200)
            return json.loads(response.get_data())

        def ids(page):
            return [row['DT_RowId'] for row in page['data']]
        everything = data()
        self.assertEqual(len(everything['data']), 5)
        page = data(draw=3, start=0, length=2)
        self.assertEqual((page['draw'], page['recordsTotal'],
                          page['recordsFiltered']), (3, 5, 5))
        chunks = [ids(data(start=start, length=2)) for start in (0, 2, 4)]
        self.assertEqual([len(chunk) for chunk in chunks], [2, 2, 1])
        self.assertEqual(sum(chunks, []), ids(everything))
        self.assertEqual(ids(data(start=5, length=2)), [])

        # the cells are [html, sort value], without the checkbox column
        cells = everything['data'][0]['c']
        self.assertEqual(len(cells), 6)
        self.assertIn('>c3</a>', cells[0][0])
        self.assertNotIn('<br', json.dumps(cells))

        found = data(**{'search[value]': 's3'})
        self.assertEqual((found['recordsTotal'], found['recordsFiltered']),
                         (5, 1))
        self.assertTrue(found['data'][0]['c'][2][1].startswith('S3:'))
        names = [row['c'][2][1].split(':')[0] for row in data(
            **{'order[0][column]': 3, 'order[0][dir]': 'desc'})['data']]
        self.assertEqual(names, ['S4', 'S3', 'S2', 'S1', 'S0'])

        for query in ({'start': 'x'}, {'draw': 'x'}, {'length': 'x'},
                      {'order[0][column]': 'x'}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(url, query_string=query)
                                 .status_code, 400)

    def test_sourceterm_data_nohiteff(self):
        """ the table can be limited to source terms with no hit efficiency,
        and still carries the component it is relative to
        """
        spec = EmissionSpec(name='e3', sources=[
            EmissionSource(name=f'S{i}', rate='1 mBq/kg')
            for i in range(3)]).save()
        c3 = Component(name='c3', mass='1 kg', specs=[spec]).save()
        HitEfficiency(source='S0', location='c3',
                      scalars=dict(v1='0.1 dru/mBq')).save()
        url = self.url('component.sourceterm_data', object=c3)

        def data(**query):
            response = self.client.get(url, query_string=query)
            self.assertEqual(response.status_code, 200)
            return json.loads(response.get_data())

        def names(page):
            return sorted(row['c'][2][1].split(':')[0]
                          for row in page['data'])
        self.assertEqual(names(data()), ['S0', 'S1', 'S2'])
        self.assertEqual(names(data(nohiteff=0)), ['S0', 'S1', 'S2'])
        page = data(nohiteff=1)
        self.assertEqual(names(page), ['S1', 'S2'])
        self.assertEqual((page['recordsTotal'], page['recordsFiltered']),
                         (3, 2))
        found = data(nohiteff=1, **{'search[value]': 's2'})
        self.assertEqual(names(found), ['S2'])
        self.assertEqual((found['recordsTotal'], found['recordsFiltered']),
                         (3, 1))
        html = self.html(self.client.get(self.url(
            'component.sourceterms', object=c3,
            relativeto=str(c3.original_id))))
        self.assertIn('/sourceterms/data?relativeto=', html)
        self.assertIn('id="filter-nohiteff"', html)

    def test_overview_data(self):
        """ the overview tables load their rows as json, also in the
        reference picker, which has no bulk actions
        """
        HitEfficiency(source='K40', location='c1',
                      scalars=dict(v1='0.1 dru/mBq')).save()
        ActivatedMaterial(name='m1').save()
        for clsname, name in (('component', 'c1'), ('emissionspec', 'e1'),
                              ('hitefficiency', 'K40'),
                              ('activatedmaterial', 'm1')):
            with self.subTest(clsname=clsname):
                url = self.url(f'{clsname}.overview')
                html = self.html(self.client.get(url))
                self.assertIn(f'data-ajax-url="{url}data"', html)
                self.assertNotIn('<tr class=', html)
                response = self.client.get(f'{url}data')
                self.assertEqual(response.status_code, 200)
                rows = json.loads(response.get_data())['data']
                self.assertTrue(rows[0]['DT_RowId'])
                self.assertTrue(any(f'>{name}</a>' in row['c'][0][0]
                                    for row in rows))
                self.assertIn('id="bulk-bar"', html)
                picker = self.html(self.client.get(url + '?embedded=1'))
                self.assertIn('data-ajax-url="'
                              f'{self.url(f"{clsname}.overview_data", embedded=1)}"',
                              picker)
                self.assertNotIn('<tr class=', picker)
                self.assertNotIn('id="bulk-bar"', picker)
                self.assertNotIn('id="bulk-delete-modal"', picker)
                # the header checkbox selects all the rows shown
                self.assertIn('class="select-all"', picker)
        # the sort value of a mass is in kg, a spec has a column per source
        mass = self.client.get(self.url('component.overview_data'))
        c1 = [row for row in json.loads(mass.get_data())['data']
              if row['c'][0][1] == 'c1'][0]
        self.assertEqual(c1['c'][4][1], 1.0)
        spec = json.loads(self.client.get(self.url(
            'emissionspec.overview_data')).get_data())['data'][0]
        self.assertEqual(spec['c'][2][1], 0.001)

    def test_sourceterm_bulk(self):
        """ the ids the table selects (the row ids) are what the bulk
        endpoints take
        """
        spec = EmissionSpec(name='e3', sources=[
            EmissionSource(name=f'S{i}', rate='1 mBq/kg')
            for i in range(3)]).save()
        c3 = Component(name='c3', mass='1 kg', specs=[spec]).save()
        rows = json.loads(self.client.get(self.url(
            'component.sourceterm_data', object=c3)).get_data())['data']
        ids = [row['DT_RowId'] for row in rows]
        count = SourceTerm.select_version('main').count
        before = count()
        for endpoint, change in (('clone', before + 2), ('delete', before)):
            response = self.client.post(
                self.url(f'component.sourceterms_bulk_{endpoint}', object=c3),
                json={'ids': ids[:2]})
            self.assertEqual(response.status_code, 200, endpoint)
            self.assertEqual(count(), change, endpoint)

    def test_spec_entries(self):
        """ confidence and location are edited with each attached spec, and
        change SourceTerm locations
        """
        c1url = self.url('component.edit', 'b', object=self.c1)
        html = self.html(self.client.get(c1url))
        self.assertIn('id="spec_entries"', html)
        self.assertIn(f'value="{self.e1.original_id}"', html)
        self.assertNotIn('location_overrides', html)
        entry = self.c1.spec_entries[0]
        data = {
            '_listfields': 'spec_entries',
            'spec_entries.id': str(entry.id),
            'spec_entries.spec': str(self.e1.original_id),
            'spec_entries.confidence_rating': '2',
            'spec_entries.location_mode': 'replace',
            'spec_entries.location': 'c1 surface',
        }
        response = self.client.post(c1url, data=data)
        self.assertEqual(response.status_code, 302)
        c1 = Component.select_version('b').get(name='c1')
        self.assertEqual([(e.spec.name, e.confidence_rating, e.location,
                           e.location_mode.value) for e in c1.spec_entries],
                         [('e1', 2, 'c1 surface', 'replace')])
        st = SourceTerm.select_version('b').get(assemblyRoot=c1)
        self.assertEqual(st.location, 'c1 surface')
        self.assertEqual(st.confidence_rating, 2)

        html = self.html(self.client.get(
            self.url('component.view', 'b', object=c1)))
        self.assertIn('id="specstable"', html)
        self.assertIn('c1 surface', html)
        html = self.html(self.client.get(
            self.url('component.sourceterms', 'b', object=c1)))
        self.assertNotIn('class="sourceterm"', html)  # the rows load later
        rows = json.loads(self.client.get(self.url(
            'component.sourceterm_data', 'b', object=c1)).get_data())['data']
        self.assertIn('title="override on c1"', rows[0]['c'][4][0])

        # an assembly places the component, and the spec still decides
        a1 = Assembly.select_version('b').get(name='a1')
        st = SourceTerm.select_version('b').get(assemblyRoot=a1)
        self.assertEqual(st.location, 'c1 surface')

        # a prefix goes in front of whatever is inherited. The view says so
        # when the component has no location of its own
        data.update({'spec_entries.location_mode': 'prefix',
                     'spec_entries.location': 'inner '})
        self.assertEqual(self.client.post(c1url, data=data).status_code, 302)
        html = self.html(self.client.get(
            self.url('component.view', 'b', object=c1)))
        self.assertIn('inner [Inherited Location]', html)
        st = SourceTerm.select_version('b').get(assemblyRoot=c1)
        self.assertEqual(st.location, 'inner c1')
        data.update({'location': 'c1 loc'})
        self.assertEqual(self.client.post(c1url, data=data).status_code, 302)
        html = self.html(self.client.get(
            self.url('component.view', 'b', object=c1)))
        self.assertIn('inner c1 loc', html)
        self.assertNotIn('[Inherited Location]', html)

        # a blank location keeps the inherited one, and blank rating clears
        data.update({'spec_entries.location': '',
                     'spec_entries.confidence_rating': ''})
        self.assertEqual(self.client.post(c1url, data=data).status_code, 302)
        c1 = Component.select_version('b').get(name='c1')
        self.assertIsNone(c1.spec_entries[0].confidence_rating)
        self.assertEqual(SourceTerm.select_version('b').get(assemblyRoot=c1)
                         .location, 'c1 loc')

        # bad values are form errors
        data.update({'spec_entries.confidence_rating': '9'})
        self.assertEqual(self.client.post(c1url, data=data).status_code, 400)
        data.update({'spec_entries.confidence_rating': '',
                     'spec_entries.location_mode': 'middle'})
        self.assertEqual(self.client.post(c1url, data=data).status_code, 400)

        # removing the row detaches the spec
        response = self.client.post(c1url, data={'_listfields':
                                                 'spec_entries'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Component.select_version('b').get(name='c1')
                         .spec_entries, [])
        # main is untouched
        self.assertEqual(Component.select_version('main').get(name='c1')
                         .spec_entries[0].location, None)

    def test_owned_specs(self):
        """ a component can create specs of its own, or copy a shared one """
        html = self.html(self.client.get(self.url('component.view', 'b',
                                                  object=self.c1)))
        self.assertIn('id="newownspec"', html)
        self.assertIn('id="newownspecmodal"', html)
        self.assertIn('name="confidence_rating"', html)
        self.assertIn('name="location"', html)
        self.assertIn('class="d-inline makespecific"', html)
        url = self.url('emissionspec.edit', 'b', type='emissionspec',
                       owner=self.c1.original_id)
        self.assertIn(f'Belongs to', self.html(self.client.get(url)))
        response = self.client.post(url, data={
            'name': 'c1 own', '_listfields': 'sources', 'sources.id': '',
            'sources.name': 'Co60', 'sources.rate': '1 mBq/kg'})
        self.assertEqual(response.status_code, 302)
        self.assertIn(self.url('component.view', 'b', object=self.c1),
                      response.headers['Location'])
        c1 = Component.select_version('b').get(name='c1')
        self.assertEqual([s.name for s in c1.specs], ['e1', 'c1 own'])
        own = c1.specs[1]
        self.assertEqual(own.owner.original_id, c1.original_id)
        self.assertEqual(own.version_tags, ['b'])

        # owned specs are only listed on request, and not in the picker
        overview = self.url('emissionspec.overview_data', 'b')
        link = '>c1 own</a>'
        self.assertNotIn(link, self.client.get(overview).get_data(as_text=True))
        self.assertNotIn(link, self.client.get(self.url(
            'emissionspec.overview_data', 'b', embedded=1)).get_data(as_text=True))
        self.assertIn(link, self.client.get(
            overview, query_string={'owned': 1}).get_data(as_text=True))

        # editing the component keeps them, and marks them
        html = self.html(self.client.get(self.url('component.edit', 'b',
                                                  object=c1)))
        self.assertIn('owned', html)
        self.assertNotIn('name="owner"', self.html(self.client.get(
            self.url('emissionspec.edit', 'b', object=own))))

        # make e1 specific
        response = self.client.post(self.url(
            'component.make_specific', 'b', object=c1,
            specid=self.e1.original_id))
        self.assertEqual(response.status_code, 302)
        c1 = Component.select_version('b').get(name='c1')
        self.assertEqual([(s.name, s.owner.name) for s in c1.specs],
                         [('e1 (c1)', 'c1'), ('c1 own', 'c1')])
        self.assertIn(self.url('emissionspec.edit', 'b', object=c1.specs[0]),
                      response.headers['Location'])
        # but not twice
        self.assertEqual(self.client.post(self.url(
            'component.make_specific', 'b', object=c1,
            specid=c1.specs[0].original_id)).status_code, 400)
        # main is untouched
        self.assertEqual([s.name for s in Component.select_version('main')
                          .get(name='c1').specs], ['e1'])

        # deleting the component deletes them
        html = self.html(self.client.get(self.url('component.delete', 'b',
                                                  object=c1)))
        self.assertIn('Its own specs are deleted', html)
        self.client.post(self.url('component.delete', 'b', object=c1))
        self.assertEqual(EmissionSpec.select_version('b')(
            owner__ne=None).count(), 0)
        self.assertEqual(EmissionSpec.select_version('b').count(), 1)

    def test_owned_spec_settings(self):
        """ the new spec modal passes confidence and location along """
        url = self.url('emissionspec.edit', 'b', type='emissionspec',
                       owner=self.c1.original_id, confidence_rating=4,
                       location_mode='suffix', location=' surface')
        response = self.client.post(url, data={
            'name': 'c1 own', '_listfields': 'sources', 'sources.id': '',
            'sources.name': 'Co60', 'sources.rate': '1 mBq/kg'})
        self.assertEqual(response.status_code, 302)
        c1 = Component.select_version('b').get(name='c1')
        entry = c1.find_spec_entry(c1.specs[1])
        self.assertEqual((entry.confidence_rating, entry.location,
                          entry.location_mode.value),
                         (4, ' surface', 'suffix'))
        self.assertIsNone(c1.spec_entries[0].confidence_rating)
        sts = SourceTerm.select_version('b')(assemblyRoot=c1,
                                             source__name='Co60')
        self.assertEqual(sts.get().location, 'c1 surface')
        # bad settings are rejected before anything is created
        for bad in ({'confidence_rating': 'high'}, {'location': 'x',
                                                    'location_mode': 'mid'}):
            url = self.url('emissionspec.edit', 'b', type='emissionspec',
                           owner=self.c1.original_id, **bad)
            self.assertEqual(self.client.post(url, data={'name': 'x'})
                             .status_code, 400)

    def import_file(self, version, data, filename):
        return self.client.post(
            self.url('component.import_', version),
            data={'file': (io.BytesIO(data), filename)},
            content_type='multipart/form-data', follow_redirects=True)

    def test_import(self):
        exported = self.client.get(self.url('component.get_json',
                                            object=self.a1)).get_data()
        vc.create_version('empty')
        response = self.import_file('empty', exported, 'a1.json')
        self.assertEqual(response.status_code, 200)
        html = self.html(response)
        self.assertIn('Imported 1 documents', html)
        self.assertIn('Dropped references', html)
        self.assertEqual(Assembly.select_version('empty').get().children, [])

        # an archive with both
        docs = [json.loads(self.client.get(self.url(
            'component.get_json', object=obj)).get_data()) for obj in
            (self.a1, self.c1)]
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            data = json.dumps(docs).encode()
            info = tarfile.TarInfo('all.json')
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        vc.create_version('empty2')
        html = self.html(self.import_file('empty2', buf.getvalue(),
                                          'all.tar.gz'))
        self.assertIn('Imported 2 documents', html)
        a1 = Assembly.select_version('empty2').get()
        self.assertEqual(a1.children[0].component.name, 'c1')
        # c1's spec isn't in empty2
        self.assertIn('Dropped references', html)

        response = self.import_file('empty2', b'nonsense', 'x.json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('not valid JSON', self.html(response))

    def test_readonly(self):
        """ tags have no buttons and refuse changes """
        html = self.html(self.client.get(self.url('component.overview',
                                                  't')))
        self.assertNotIn('id="newcomponent"', html)
        self.assertNotIn('id="import"', html)
        html = self.html(self.client.get(self.url('component.view', 't',
                                                  object=self.c1)))
        self.assertNotIn('id="cloneform"', html)
        for endpoint, kwargs in (('component.edit', {}),
                                 ('component.clone', {'object': self.c1}),
                                 ('component.import_', {})):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(self.url(endpoint, 't',
                                                     **kwargs))
                self.assertEqual(response.status_code, 403)
        html = self.html(self.client.get(self.url('component.overview',
                                                  'b')))
        self.assertIn('id="newcomponent"', html)

    def test_other_versions(self):
        c1b = Component.select_version('b').get(name='c1')
        c1b.description = 'edited'
        c1b.save()
        html = self.html(self.client.get(self.url('component.view', 'b',
                                                  object=self.c1)))
        self.assertIn('id="otherversions"', html)
        for tag in ('main', 't'):
            url = self.url('component.view', tag, object=self.c1)
            self.assertIn(f'href="{url}"', html)
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertIn('text-bg-secondary">tag', html)
        for endpoint, obj in (('emissionspec.view', self.e1),):
            html = self.html(self.client.get(self.url(endpoint, 'b',
                                                      object=obj)))
            self.assertIn(self.url(endpoint, 't', object=obj), html)
