""" Jobs in the web application: configuration, the status page and
submitting a job from a request """
import datetime
import threading
import unittest
from bson import ObjectId
import mongoengine
from bgexplorer import jobs
from bgexplorer.jobs import models
from bgexplorer.application import jobs as appjobs
from bgexplorer.application.app import create_app
from bgexplorer.models.users import Role
from bgexplorer.versioning.verdoc import current_user_name
from tests.dbutil import TEST_MONGODB_URI
from tests.test_app_components import AppTestCase, reset_database
from tests.test_auth import AuthTestCase

HOLD = threading.Event()


@jobs.job_kind('app_quick', label='Quick test')
def _quick(params):
    return {'messages': [['Quick job done', 'success']],
            'redirect': '/explore/main/'}


@jobs.job_kind('app_hold', label='Held test')
def _hold(params):
    jobs.progress(1, 2, stage='holding')
    HOLD.wait(10)
    return {'messages': [['Held job done', 'success']]}


@jobs.job_kind('app_fail', label='Failing test')
def _fail(params):
    raise ValueError('that did not work\nsecond line')


@jobs.job_kind('app_whoami')
def _whoami(params):
    return {'user': current_user_name()}


def add_test_route(app):
    """ POST /test-submit/<kind> submits a job like an edit handler would """
    import flask

    @app.post('/test-submit/<kind>')
    def test_submit(kind):
        return appjobs.submit_and_redirect(kind, {}, 'main', '/explore/main/')

    @app.post('/test-wait/<kind>')
    def test_wait(kind):
        job = appjobs.submit_and_wait(kind, {}, 'main')
        return flask.jsonify(status=job.status, user=job.user)


def clear_jobs():
    models.Job.drop_collection()
    models.Job.ensure_indexes()


def put_job(kind='app_quick', status='queued', user=None, **fields):
    """ A job in the database that no runner touches """
    job_id = models.submit(kind, {}, scope='main', user=user)
    if status != 'queued':
        fields['status'] = status
    if fields:
        models.Job._get_collection().update_one(
            {'_id': ObjectId(job_id)}, {'$set': fields})
    return job_id


class TestConfig(unittest.TestCase):
    def tearDown(self):
        mongoengine.disconnect()

    def make(self, **config):
        reset_database()
        return create_app(config={'MONGODB_URI': TEST_MONGODB_URI,
                                  'LOGIN_DISABLED': True,
                                  'WTF_CSRF_ENABLED': False, **config})

    def test_eager_when_testing_else_threads(self):
        app = self.make(TESTING=True)
        self.assertEqual(app.config['JOBS_MODE'], 'eager')
        self.assertIs(app.extensions['jobs'], appjobs.runner(app))
        self.assertEqual(app.extensions['jobs'].mode, 'eager')
        mongoengine.disconnect()
        app = self.make(TESTING=False)
        self.assertEqual(app.config['JOBS_MODE'], 'thread')
        self.assertEqual(app.extensions['jobs'].mode, 'thread')
        self.assertTrue(1 <= app.config['JOBS_WORKERS'] <= 4)

    def test_settings_are_passed_on(self):
        app = self.make(TESTING=True, JOBS_MODE='thread', JOBS_WORKERS=3,
                        JOBS_LEASE_SECONDS=30, JOBS_POLL_SECONDS=0.25,
                        JOBS_SHUTDOWN_SECONDS=4, JOBS_KEEP_DAYS=2)
        runner = app.extensions['jobs']
        self.assertEqual((runner.mode, runner.workers, runner.lease_seconds,
                          runner.poll_seconds, runner.shutdown_seconds,
                          runner.keep_days), ('thread', 3, 30, 0.25, 4, 2))

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            self.make(TESTING=True, JOBS_MODE='fibers')

    def test_the_runner_starts_with_the_first_request(self):
        app = self.make(TESTING=False, JOBS_POLL_SECONDS=0.05)
        runner = app.extensions['jobs']
        try:
            self.assertFalse(runner.started)
            self.assertEqual(app.test_client().get('/').status_code, 200)
            self.assertTrue(runner.started)
        finally:
            runner.stop(wait=False)

    def test_eager_mode_does_not_start_threads(self):
        app = self.make(TESTING=True)
        before = threading.active_count()
        self.assertEqual(app.test_client().get('/').status_code, 200)
        self.assertEqual(threading.active_count(), before)


class TestSafeNext(unittest.TestCase):
    def test_only_paths_of_this_site(self):
        for target in ('/', '/explore/main/', '/x?y=1#z'):
            self.assertEqual(appjobs.safe_next(target), target)
        for target in (None, '', 'x', '//evil.example', 'https://evil.example',
                       '/\\evil.example', 'javascript:alert(1)'):
            self.assertIsNone(appjobs.safe_next(target), target)


class TestStatusPage(AppTestCase):
    def setUp(self):
        super().setUp()
        clear_jobs()

    def page(self, job_id, **query):
        return self.client.get(f'/jobs/{job_id}', query_string=query)

    def test_running_job_shows_progress(self):
        job_id = put_job('app_hold', 'running', **{
            'progress.done': 5, 'progress.total': 10,
            'progress.stage': 'holding', 'progress.message': 'halfway'})
        response = self.page(job_id, next='/explore/main/')
        self.assertEqual(response.status_code, 200)
        html = self.html(response)
        self.assertIn('Held test', html)
        self.assertIn('id="job-progress"', html)
        self.assertIn('width: 50%', html)
        self.assertIn('holding', html)
        self.assertIn('halfway', html)
        # the page watches the job and goes on to where it was asked to
        self.assertIn(f'/jobs/{job_id}/status', html)
        self.assertIn('http-equiv="refresh"', html)
        self.assertIn(f'/jobs/{job_id}/cancel?next=/explore/main/', html)

    def test_queued_job_waits(self):
        job_id = put_job('app_hold')
        html = self.html(self.page(job_id))
        self.assertIn('Waiting to start', html)
        self.assertIn('id="job-progress"', html)
        self.assertIn('id="job-cancel"', html)

    def test_unknown_total_has_no_percentage(self):
        job_id = put_job('app_hold', 'running')
        html = self.html(self.page(job_id))
        self.assertIn('progress-bar-striped', html)
        self.assertNotIn('width: 0%', html)

    def test_unknown_total_says_so(self):
        """ a bar that is full from the start doesn't look like progress """
        job_id = put_job('app_hold', 'running')
        html = self.html(self.page(job_id))
        self.assertIn('id="job-no-estimate"', html)
        self.assertNotIn('aria-valuenow="', html)
        job_id = put_job('app_hold', 'running', **{
            'progress.done': 5, 'progress.total': 10})
        self.assertNotIn('id="job-no-estimate"', self.html(self.page(job_id)))

    def test_elapsed_time_is_kept_up_to_date(self):
        """ the page asks for it, and the status gives it """
        started = models.utcnow() - datetime.timedelta(seconds=75)
        job_id = put_job('app_hold', 'running', started=started)
        html = self.html(self.page(job_id))
        self.assertIn('id="job-elapsed"', html)
        self.assertIn('id="job-since"', html)
        self.assertIn('job.elapsed', html)
        data = self.client.get(f'/jobs/{job_id}/status').get_json()
        self.assertRegex(data['elapsed'], r'^1 min 1\d s$')
        self.assertEqual(data['since'], 'it started')
        job_id = put_job('app_hold')
        data = self.client.get(f'/jobs/{job_id}/status').get_json()
        self.assertEqual(data['since'], 'it was queued')
        self.assertRegex(data['elapsed'], r'^\d+ s$')

    def test_succeeded_job_goes_on_to_where_it_says(self):
        job_id = put_job('app_quick', 'succeeded', result={
            'messages': [['All saved', 'success']], 'redirect': '/elsewhere'})
        response = self.page(job_id, next='/explore/main/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/elsewhere')
        self.assertIn('All saved', self.html(self.client.get('/')))

    def test_succeeded_job_without_a_redirect_goes_on_to_next(self):
        job_id = put_job('app_quick', 'succeeded', result={
            'messages': [['Done', 'success']]})
        response = self.page(job_id, next='/explore/main/')
        self.assertTrue(response.headers['Location'].endswith('/explore/main/'))
        job_id = put_job('app_quick', 'succeeded')
        self.assertEqual(self.page(job_id).headers['Location'], '/')

    def test_next_must_stay_on_this_site(self):
        job_id = put_job('app_quick', 'succeeded')
        for target in ('//evil.example', 'https://evil.example', '/\\evil'):
            with self.subTest(next=target):
                response = self.page(job_id, next=target)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers['Location'], '/')

    def test_failed_job_shows_its_error(self):
        for status in ('failed', 'cancelled', 'abandoned'):
            with self.subTest(status=status):
                job_id = put_job('app_fail', status, error='that did not work',
                                 error_type='ValueError',
                                 traceback='Traceback (most recent call last)')
                response = self.page(job_id, next='/explore/main/')
                self.assertEqual(response.status_code, 200)
                html = self.html(response)
                self.assertIn('id="job-error"', html)
                self.assertIn('that did not work', html)
                self.assertNotIn('id="job-progress"', html)
                self.assertIn('href="/explore/main/"', html)

    def test_unknown_job(self):
        self.assertEqual(self.page(str(ObjectId())).status_code, 404)
        self.assertEqual(self.page('nonsense').status_code, 404)

    def test_status_json(self):
        job_id = put_job('app_hold', 'running', **{
            'progress.done': 5, 'progress.total': 10,
            'progress.stage': 'holding'})
        response = self.client.get(f'/jobs/{job_id}/status')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['id'], job_id)
        self.assertEqual(data['status'], 'running')
        self.assertEqual(data['kind'], 'app_hold')
        self.assertEqual(data['label'], 'Held test')
        self.assertEqual(data['percent'], 50)
        self.assertEqual(data['stage'], 'holding')
        self.assertFalse(data['finished'])
        job_id = put_job('app_hold', 'running')
        self.assertIsNone(self.client.get(f'/jobs/{job_id}/status')
                          .get_json()['percent'])
        job_id = put_job('app_quick', 'succeeded', result={'x': 1})
        data = self.client.get(f'/jobs/{job_id}/status').get_json()
        self.assertTrue(data['finished'])
        self.assertEqual(data['result'], {'x': 1})

    def test_cancel_a_queued_job(self):
        job_id = put_job('app_hold')
        response = self.client.post(f'/jobs/{job_id}/cancel',
                                    query_string={'next': '/explore/main/'})
        self.assertEqual(response.status_code, 302)
        self.assertIn(f'/jobs/{job_id}', response.headers['Location'])
        self.assertIn('next=/explore/main/', response.headers['Location'])
        self.assertEqual(models.get(job_id).status, 'cancelled')

    def test_cancel_a_running_job_only_asks(self):
        job_id = put_job('app_hold', 'running')
        self.client.post(f'/jobs/{job_id}/cancel')
        job = models.get(job_id)
        self.assertEqual(job.status, 'running')
        self.assertTrue(job.cancel_requested)

    def test_cancel_needs_a_post(self):
        job_id = put_job('app_hold')
        self.assertEqual(self.client.get(f'/jobs/{job_id}/cancel')
                         .status_code, 405)


class TestStatusPageAccess(AuthTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        add_test_route(cls.app)

    def setUp(self):
        super().setUp()
        clear_jobs()
        self.make_user('owner', Role.editor)
        self.make_user('other', Role.editor)
        self.make_user('watcher', Role.viewer)
        self.make_user('boss', Role.site_admin)
        self.job_id = put_job('app_fail', 'failed', user='owner',
                              error='that did not work',
                              traceback='Traceback (most recent call last)')

    def as_user(self, name):
        client = self.app.test_client()
        self.login(name, client=client)
        return client

    def test_the_owner_sees_the_error_but_not_the_traceback(self):
        response = self.as_user('owner').get(f'/jobs/{self.job_id}')
        self.assertEqual(response.status_code, 200)
        html = self.html(response)
        self.assertIn('that did not work', html)
        self.assertNotIn('Traceback (most recent call last)', html)

    def test_an_administrator_sees_every_job_with_its_traceback(self):
        response = self.as_user('boss').get(f'/jobs/{self.job_id}')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Traceback (most recent call last)', self.html(response))

    def test_other_users_get_a_404(self):
        for name in ('other', 'watcher'):
            client = self.as_user(name)
            for path in (f'/jobs/{self.job_id}', f'/jobs/{self.job_id}/status'):
                with self.subTest(user=name, path=path):
                    self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.post(f'/jobs/{self.job_id}/cancel')
                             .status_code, 404)

    def test_anonymous_users_log_in_first(self):
        self.set_anon_view(False)
        response = self.client.get(f'/jobs/{self.job_id}')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])

    def test_a_viewer_cannot_cancel_even_their_own_job(self):
        job_id = put_job('app_hold', user='watcher')
        client = self.as_user('watcher')
        self.assertEqual(client.get(f'/jobs/{job_id}').status_code, 200)
        self.assertEqual(client.post(f'/jobs/{job_id}/cancel').status_code, 403)
        self.assertEqual(models.get(job_id).status, 'queued')

    def test_the_owner_can_cancel(self):
        job_id = put_job('app_hold', user='owner')
        self.as_user('owner').post(f'/jobs/{job_id}/cancel')
        self.assertEqual(models.get(job_id).status, 'cancelled')

    def test_jobs_record_and_use_the_requesting_user(self):
        client = self.as_user('owner')
        response = client.post('/test-wait/app_whoami')
        self.assertEqual(response.get_json(),
                         {'status': 'succeeded', 'user': 'owner'})
        job = models.Job.objects(kind='app_whoami').get()
        self.assertEqual(job.user, 'owner')
        self.assertEqual(job.result, {'user': 'owner'})
        self.assertEqual(job.origin, '/test-wait/app_whoami')


class TestSubmitEager(AppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        add_test_route(cls.app)

    def setUp(self):
        super().setUp()
        clear_jobs()

    def test_a_quick_job_redirects_straight_away_with_its_flash(self):
        response = self.client.post('/test-submit/app_quick')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/explore/main/'))
        self.assertIn('Quick job done', self.html(self.client.get('/')))

    def test_a_job_that_fails_at_once_goes_back_with_the_error(self):
        response = self.client.post('/test-submit/app_fail')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/explore/main/'))
        html = self.html(self.client.get('/'))
        self.assertIn('that did not work', html)
        self.assertIn('alert-danger', html)
        self.assertNotIn('second line', html)

    def test_the_job_remembers_who_and_where(self):
        self.client.post('/test-submit/app_quick')
        job = models.Job.objects.get()
        self.assertEqual((job.kind, job.scope, job.status),
                         ('app_quick', 'main', 'succeeded'))
        self.assertEqual(job.origin, '/test-submit/app_quick')

    def test_wait_returns_the_job(self):
        response = self.client.post('/test-wait/app_fail')
        self.assertEqual(response.get_json()['status'], 'failed')


class TestSubmitThreads(AppTestCase):
    """ With a thread pool, a job that takes longer than the grace period
    leaves the user on the status page """
    @classmethod
    def setUpClass(cls):
        reset_database()
        cls.app = create_app(config={
            'MONGODB_URI': TEST_MONGODB_URI, 'TESTING': True,
            'LOGIN_DISABLED': True, 'WTF_CSRF_ENABLED': False,
            'JOBS_MODE': 'thread', 'JOBS_WORKERS': 2,
            'JOBS_GRACE_SECONDS': 0.3, 'JOBS_POLL_SECONDS': 0.05})
        add_test_route(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions['jobs'].stop(wait=False)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        clear_jobs()
        HOLD.clear()

    def tearDown(self):
        HOLD.set()
        runner = self.app.extensions['jobs']
        deadline = 5
        while runner.running_ids() and deadline > 0:
            threading.Event().wait(0.05)
            deadline -= 0.05

    def test_a_job_inside_the_grace_period_is_like_a_synchronous_one(self):
        response = self.client.post('/test-submit/app_quick')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/explore/main/'))
        self.assertIn('Quick job done', self.html(self.client.get('/')))

    def test_a_slow_job_leads_to_the_status_page(self):
        response = self.client.post('/test-submit/app_hold')
        self.assertEqual(response.status_code, 303)
        job = models.Job.objects.get()
        self.assertEqual(response.headers['Location'],
                         f'/jobs/{job.id}?next=/explore/main/')
        self.assertIn(job.status, ('queued', 'running'))
        page = self.client.get(response.headers['Location'])
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="job-progress"', self.html(page))
        HOLD.set()
        done = self.app.extensions['jobs'].wait(str(job.id), 5)
        self.assertEqual(done.status, 'succeeded')
        response = self.client.get(response.headers['Location'])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/explore/main/'))
        self.assertIn('Held job done', self.html(self.client.get('/')))

    def test_wait_gives_up_after_the_grace_period(self):
        response = self.client.post('/test-wait/app_hold')
        self.assertIn(response.get_json()['status'], ('queued', 'running'))


if __name__ == '__main__':
    unittest.main()
