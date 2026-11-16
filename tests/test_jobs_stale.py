""" Views of a version with a job in progress still show the results so far,
flagged as possibly stale """
import datetime
import unittest
from bson import ObjectId
from bgexplorer.application.app import create_app
from bgexplorer.jobs import models
from bgexplorer.models.component import Component
from bgexplorer.versioning import control as vc
from tests.dbutil import TEST_MONGODB_URI
from tests.test_app import reset_database
from tests.test_app_components import AppTestCase
from tests.test_jobs_app import clear_jobs, put_job

HEADER = 'X-Bgexplorer-Stale'


class StaleCase(AppTestCase):
    def setUp(self):
        super().setUp()
        clear_jobs()
        vc.create_version('main')
        self.c1 = Component(name='c1', mass='1 kg').save()
        vc.create_version('other', 'main')

    def overview(self, version='main'):
        return self.client.get(self.url('overview', version))

    def banner(self, response) -> bool:
        return 'id="stale-banner"' in self.html(response)

    def banner_html(self, response) -> str:
        html = self.html(response)
        start = html.index('id="stale-banner"')
        return html[start:html.index('</div>', start)]


class TestBanner(StaleCase):
    def test_no_job_no_banner(self):
        response = self.overview()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.banner(response))

    def test_a_running_job_flags_every_view_of_its_version(self):
        job_id = put_job('save_document', 'running', **{
            'progress.done': 3, 'progress.total': 4})
        for endpoint, values in (('overview', {}),
                                 ('component.overview', {}),
                                 ('component.view', {'object': self.c1})):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(self.url(endpoint, **values))
                self.assertEqual(response.status_code, 200)
                html = self.banner_html(response)
                self.assertIn('Saving a document', html)
                self.assertIn('75%', html)
                self.assertIn(f'href="/jobs/{job_id}', html)
                self.assertIn('may not include', html)

    def test_the_banner_is_inside_the_messages(self):
        put_job('save_document', 'running')
        html = self.html(self.overview())
        self.assertLess(html.index('id="messages"'),
                        html.index('id="stale-banner"'))
        self.assertLess(html.index('id="stale-banner"'),
                        html.index('id="pagecontent"'))

    def test_without_a_percentage_it_is_left_out(self):
        put_job('save_document', 'running')
        html = self.banner_html(self.overview())
        self.assertNotIn('%', html)

    def test_a_queued_job_flags_too(self):
        put_job('save_document')
        self.assertTrue(self.banner(self.overview()))

    def test_every_job_is_listed(self):
        first = put_job('save_document', 'running')
        second = put_job('delete_document')
        html = self.banner_html(self.overview())
        self.assertIn(f'/jobs/{first}', html)
        self.assertIn(f'/jobs/{second}', html)

    def test_other_versions_are_not_flagged(self):
        put_job('save_document', 'running')
        self.assertFalse(self.banner(self.overview('other')))

    def test_finished_jobs_do_not_flag(self):
        for status in ('succeeded', 'failed', 'cancelled', 'abandoned'):
            put_job('save_document', status)
        self.assertFalse(self.banner(self.overview()))

    def test_the_banner_goes_when_the_job_is_done(self):
        job_id = put_job('save_document', 'running')
        self.assertTrue(self.banner(self.overview()))
        models.Job._get_collection().update_one(
            {'_id': ObjectId(job_id)}, {'$set': {'status': 'succeeded'}})
        self.assertFalse(self.banner(self.overview()))

    def test_a_job_abandoned_by_the_reaper_stops_flagging(self):
        job_id = put_job('app_hold', 'running', attempts=1, lease={
            'owner': 'dead', 'expires': models.utcnow()
            - datetime.timedelta(minutes=5)})
        self.assertTrue(self.banner(self.overview()))
        self.assertEqual(models.reap(), [job_id])
        self.assertEqual(models.get(job_id).status, 'abandoned')
        self.assertFalse(self.banner(self.overview()))

    def test_the_status_page_and_other_pages_are_not_flagged(self):
        job_id = put_job('save_document', 'running')
        for path in (f'/jobs/{job_id}', f'/jobs/{job_id}/status', '/'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('stale-banner', self.html(response))

    def test_the_job_of_an_unregistered_kind_still_flags(self):
        job_id = put_job('save_document', 'running')
        models.Job._get_collection().update_one(
            {'_id': ObjectId(job_id)}, {'$set': {'kind': 'kind_from_the_future'}})
        self.assertIn('kind_from_the_future',
                      self.banner_html(self.overview()))

    def test_the_lookup_uses_the_scope_and_status_index(self):
        explanation = models.Job._get_collection().find(
            {'scope': 'main', 'status': {'$in': ['queued', 'running']}}
        ).sort([('created', 1), ('_id', 1)]).explain()
        self.assertIn('IXSCAN', str(explanation['queryPlanner']['winningPlan']))
        self.assertNotIn('COLLSCAN',
                         str(explanation['queryPlanner']['winningPlan']))


class TestApi(StaleCase):
    def get(self, path):
        return self.client.get(f'/api/v1{path}')

    def test_no_job_no_header(self):
        response = self.get('/versions/main')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(HEADER, response.headers)
        self.assertEqual(response.get_json()['pending_jobs'], [])

    def test_the_header_lists_the_jobs(self):
        first = put_job('save_document', 'running')
        for path in ('/versions/main', '/versions/main/merge?source=other'):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers[HEADER], first)
        second = put_job('delete_document')
        self.assertEqual(self.get('/versions/main').headers[HEADER],
                         f'{first},{second}')

    def test_other_versions_have_no_header(self):
        put_job('save_document', 'running')
        self.assertNotIn(HEADER, self.get('/versions/other').headers)

    def test_head_requests_get_it_too(self):
        job_id = put_job('save_document', 'running')
        response = self.client.head('/api/v1/versions/main')
        self.assertEqual(response.headers[HEADER], job_id)

    def test_changes_do_not_get_it(self):
        put_job('save_document', 'running')
        response = self.client.patch('/api/v1/versions/main',
                                     json={'description': 'new'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(HEADER, response.headers)

    def test_the_version_lists_its_pending_jobs(self):
        first = put_job('save_document', 'running', **{
            'progress.done': 1, 'progress.total': 4})
        second = put_job('delete_document')
        put_job('save_document', 'succeeded')
        put_job('save_document', 'running', scope='other')
        data = self.get('/versions/main').get_json()
        self.assertEqual(data['pending_jobs'], [
            {'id': first, 'kind': 'save_document', 'status': 'running',
             'percent': 25},
            {'id': second, 'kind': 'delete_document', 'status': 'queued',
             'percent': None}])

    def test_html_pages_outside_a_version_have_no_header(self):
        put_job('save_document', 'running')
        self.assertNotIn(HEADER, self.client.get('/').headers)


class TestBannerEndToEnd(AppTestCase):
    """ With a pool of threads, an edit held up by a lock flags the version
    until it has been done """
    @classmethod
    def setUpClass(cls):
        reset_database()
        cls.app = create_app(config={
            'MONGODB_URI': TEST_MONGODB_URI, 'TESTING': True,
            'LOGIN_DISABLED': True, 'WTF_CSRF_ENABLED': False,
            'JOBS_MODE': 'thread', 'JOBS_WORKERS': 2,
            'JOBS_GRACE_SECONDS': 0.1, 'JOBS_POLL_SECONDS': 0.05})

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions['jobs'].stop(wait=False)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        clear_jobs()
        vc.create_version('main')
        self.c1 = Component(name='c1', mass='1 kg').save()

    def test_a_slow_edit_flags_the_version_until_it_is_done(self):
        from bgexplorer.versioning.locks import hold_locks
        overview = self.url('overview')
        edit = self.url('component.edit', object=self.c1)
        with hold_locks(['main'], 'test'):
            response = self.client.post(edit, data={'description': 'later'})
            self.assertEqual(response.status_code, 303)
            html = self.html(self.client.get(overview))
            self.assertIn('id="stale-banner"', html)
            self.assertIn('Saving a document', html)
        job = models.Job.objects.get()
        self.assertEqual(self.app.extensions['jobs'].wait(str(job.id), 10)
                         .status, 'succeeded')
        self.assertNotIn('stale-banner', self.html(self.client.get(overview)))


if __name__ == '__main__':
    unittest.main()
