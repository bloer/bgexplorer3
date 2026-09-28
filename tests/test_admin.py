""" The site admin pages and the maintenance functions behind them """
import io
from bson import ObjectId
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.settings import ApplicationSettings, VersionSettings
from bgexplorer.models.sourceterm import SourceTerm, CalculatedResults
from bgexplorer.models import versioncontrol as vc
from bgexplorer.models import maintenance
from tests.test_app_components import AppTestCase

PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 16


class TestAdmin(AppTestCase):
    def setUp(self):
        super().setUp()
        ApplicationSettings.drop_collection()
        vc.create_version('main')
        HitEfficiency(source='Th232', location='c1',
                      scalars=dict(v1='0.1 +- 0.01 dru/mBq')).save()
        HitEfficiency(source='K40', location='a1',
                      scalars=dict(v1='<0.2 dru/mBq')).save()
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='Th232', rate='10 +- 1 mBq/kg'),
            EmissionSource(name='K40', rate='<25 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='2 kg', location='c1',
                            specs=[self.e1]).save()
        self.c2 = Component(name='c2', mass='1 kg', specs=[self.e1]).save()
        self.a1 = Assembly(name='a1', location='a1', children=[
            Placement(component=self.c1, weight=2),
            Placement(component=self.c2)]).save()
        vc.create_tag('t', 'main')

    def total(self, version='main'):
        a1 = Assembly.select_version(version).get(name='a1')
        results = CalculatedResults.for_object(a1, save=False, spectra=False,
                                               cache=False)
        return {k: str(v) for k, v in results.scalars.items()}

    def test_pages(self):
        for endpoint in ('admin.index', 'admin.settings',
                         'admin.maintenance_page'):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(self.url(endpoint))
                self.assertEqual(response.status_code, 200)
        self.assertIn('id="adminlink"',
                      self.html(self.client.get(self.url('index'))))

    def test_settings(self):
        url = self.url('admin.settings')
        response = self.client.post(url, data=dict(
            org_name='Test Lab', org_url='https://example.org',
            allow_anon_view='false',
            org_logo=(io.BytesIO(PNG), 'logo.png', 'image/png')))
        self.assertEqual(response.status_code, 302)
        settings = ApplicationSettings.objects.get()
        self.assertEqual(settings.org_name, 'Test Lab')
        self.assertEqual(settings.org_url, 'https://example.org')
        self.assertFalse(settings.allow_anon_view)
        self.assertEqual(settings.org_logo, PNG)

        logo = self.client.get(self.url('admin.logo'))
        self.assertEqual(logo.status_code, 200)
        self.assertEqual(logo.mimetype, 'image/png')
        self.assertEqual(logo.data, PNG)
        # the navbar shows the org brand instead of the default
        page = self.html(self.client.get(self.url('index')))
        self.assertIn('id="orgbrand" href="https://example.org"', page)
        self.assertNotIn('logopnnl.png', page)

        # a bad url is reported and nothing is saved
        response = self.client.post(url, data=dict(org_url='not a url'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('id="toperror"', self.html(response))
        self.assertEqual(ApplicationSettings.objects.get().org_url,
                         'https://example.org')
        # a non-image logo is refused
        response = self.client.post(url, data=dict(
            org_logo=(io.BytesIO(b'hello'), 'x.txt', 'text/plain')))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ApplicationSettings.objects.get().org_logo, PNG)

        # removing the logo; other fields are kept
        response = self.client.post(url, data=dict(remove_logo='true'))
        self.assertEqual(response.status_code, 302)
        settings = ApplicationSettings.objects.get()
        self.assertIsNone(settings.org_logo)
        self.assertEqual(settings.org_name, 'Test Lab')
        self.assertEqual(self.client.get(self.url('admin.logo')).status_code,
                         404)
        page = self.html(self.client.get(self.url('index')))
        self.assertIn('>Test Lab</a>', page)
        self.assertEqual(ApplicationSettings.objects.count(), 1)

    def test_clear_cache(self):
        before = self.total()
        self.assertIn('v1', before)
        CalculatedResults.for_object(self.a1, save=True)
        self.assertGreater(CalculatedResults.objects.count(), 0)
        response = self.client.post(self.url('admin.clear_cache'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CalculatedResults.objects.count(), 0)
        self.assertEqual(self.total(), before)

    def test_rebuild(self):
        before = self.total()
        self.assertIn('v1', before)
        count = SourceTerm.select_version('main').count()
        self.assertGreater(count, 0)
        SourceTerm.select_version('main').delete()
        self.assertEqual(SourceTerm.select_version('main').count(), 0)
        response = self.client.post(self.url('admin.rebuild'),
                                    data=dict(version='main'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SourceTerm.select_version('main').count(), count)
        self.assertEqual(self.total(), before)
        # the tag is unchanged and can't be rebuilt
        self.assertEqual(SourceTerm.select_version('t').count(), count)
        response = self.client.post(self.url('admin.rebuild'),
                                    data=dict(version='t'))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(self.url('admin.rebuild'),
                                    data=dict(version='nope'))
        self.assertEqual(response.status_code, 404)

    def test_orphans(self):
        self.assertFalse(maintenance.find_orphans())
        self.assertIn('No orphaned documents',
                      self.html(self.client.get(
                          self.url('admin.maintenance_page'))))
        # an untagged component, a document tagged with a missing version,
        # and a source term whose component is gone
        coll = Component._get_collection()
        untagged = ObjectId()
        coll.insert_one({'_id': untagged, 'original_id': untagged,
                         'name': 'lost', 'version_tags': [],
                         '_cls': 'Component'})
        coll.update_one({'_id': self.c2.id},
                        {'$push': {'version_tags': 'ghost'}})
        stcoll = SourceTerm._get_collection()
        st = stcoll.find_one({'version_tags': 'main'})
        st['_id'] = ObjectId()
        st['original_id'] = st['_id']
        st['version_tags'] = ['main']
        st['assemblyRoot'] = ObjectId()
        stcoll.insert_one(st)
        stcount = SourceTerm.select_version('main').count()

        orphans = maintenance.find_orphans()
        self.assertEqual(orphans.untagged, {'Component': [untagged]})
        self.assertEqual(orphans.unknown_tags, ['ghost'])
        self.assertEqual(orphans.sourceterms, {'main': [st['_id']]})
        page = self.html(self.client.get(self.url('admin.maintenance_page')))
        self.assertIn('id="deleteorphans"', page)
        self.assertIn('ghost', page)

        response = self.client.post(self.url('admin.delete_orphans'))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(maintenance.find_orphans())
        self.assertIsNone(coll.find_one({'_id': untagged}))
        self.assertIsNone(stcoll.find_one({'_id': st['_id']}))
        self.assertEqual(SourceTerm.select_version('main').count(),
                         stcount - 1)
        self.assertNotIn('ghost',
                         coll.find_one({'_id': self.c2.id})['version_tags'])
        # real documents are untouched
        self.assertEqual(Component.select_version('main').count(), 3)
        self.assertEqual(Component.select_version('t').count(), 3)

    def test_stats(self):
        stats = maintenance.database_stats()
        self.assertEqual(set(stats['versions']), {'main', 't'})
        self.assertEqual(stats['versions']['main']['counts']['Component'],
                         {'total': 3, 'unique': 0})
        self.assertFalse(stats['versions']['t']['editable'])
        self.assertEqual(stats['collections']['Component']['count'], 3)
        self.assertIn('CalculatedResults', stats['collections'])

    def test_version_named_admin(self):
        """ version routes are under /explore, so can't collide """
        vc.create_version('admin', 'main')
        url = self.url('component.overview', version='admin')
        self.assertTrue(url.startswith('/explore/admin/'))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('c1', self.html(response))
        self.assertEqual(self.client.get('/admin/').status_code, 200)
        self.assertIn('Site administration',
                      self.html(self.client.get('/admin/')))
