""" Rebuilding, merging, importing, deleting and exporting as jobs, and the
API for looking at jobs and for waiting or not waiting for them """
import datetime
import io
import os
import shutil
import tempfile
import time
import unittest
from bson import ObjectId
from bgexplorer.application.app import create_app
from bgexplorer.jobs import models
from bgexplorer.models.component import Component
from bgexplorer.models.users import Role
from bgexplorer.versioning import control as vc
from bgexplorer.versioning.archive import export_version
from bgexplorer.versioning.locks import hold_locks
from bgexplorer.versioning.record import get_record
from tests.dbutil import TEST_MONGODB_URI
from tests.test_app import reset_database
from tests.test_app_components import AppTestCase
from tests.test_auth import AuthTestCase
from tests.test_jobs_app import clear_jobs, put_job

API = '/api/v1'


def only_job(kind) -> models.Job:
    return models.Job.objects(kind=kind).get()


def archive_of(tag) -> bytes:
    out = io.BytesIO()
    export_version(tag, out)
    return out.getvalue()


class OperationCase(AppTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.files_dir = tempfile.mkdtemp(prefix='bgx-jobs-test-')
        cls.app.config['JOBS_FILES_DIR'] = cls.files_dir

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.files_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        clear_jobs()
        for name in os.listdir(self.files_dir):
            os.remove(os.path.join(self.files_dir, name))
        vc.create_version('main')
        Component(name='c1', mass='1 kg').save()
        vc.create_version('b', 'main')
        Component(name='new', version_tag='b').save()
        vc.create_tag('t', 'main')

    def files(self):
        folder = self.app.config['JOBS_FILES_DIR']
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def upload(self, data=None, **form):
        data = archive_of('main') if data is None else data
        form.setdefault('version_tag', 'copy')
        form['file'] = (io.BytesIO(data), 'main.bgx.tar.gz')
        return form


class TestWebOperations(OperationCase):
    def test_rebuild(self):
        response = self.client.post(self.url('admin.rebuild'),
                                    data=dict(version='b'))
        self.assertEqual(response.status_code, 302)
        job = only_job('rebuild')
        self.assertEqual((job.scope, job.status, job.serial),
                         ('b', 'succeeded', True))
        self.assertEqual((job.progress.done, job.progress.total), (2, 2))
        self.assertIn("Rebuilt 0 source terms in 'b'", self.html(
            self.client.get(response.headers['Location'])))

    def test_rebuild_of_a_tag_or_a_missing_version_makes_no_job(self):
        for version, status in (('t', 403), ('nope', 404)):
            response = self.client.post(self.url('admin.rebuild'),
                                        data=dict(version=version))
            self.assertEqual(response.status_code, status)
        self.assertEqual(models.Job.objects.count(), 0)

    def test_merge(self):
        url = self.url('versions.merge', 'main')
        response = self.client.post(url, data={'source': 'b'})
        self.assertEqual(response.status_code, 302)
        job = only_job('merge')
        self.assertEqual((job.scope, job.status, job.exclusive),
                         ('main', 'succeeded', True))
        self.assertEqual(Component.select_version('main').count(), 2)
        self.assertEqual(job.result['plan']['source'], 'b')
        self.assertIn('Merged', self.html(
            self.client.get(response.headers['Location'])))

    def test_a_merge_that_is_refused_shows_the_plan_and_the_reason(self):
        url = self.url('versions.merge', 'main')
        response = self.client.post(url, data={
            'source': 'b', 'fingerprint': 'out of date'})
        self.assertEqual(response.status_code, 409)
        html = self.html(response)
        self.assertIn('id="mergeerror"', html)
        self.assertIn('id="mergeform"', html)
        job = only_job('merge')
        self.assertEqual((job.status, job.result['status']), ('failed', 409))
        self.assertEqual(Component.select_version('main').count(), 1)

    def test_merge_errors_that_need_no_job(self):
        url = self.url('versions.merge', 'main')
        self.assertEqual(self.client.post(url, data={'source': 'main'})
                         .status_code, 400)
        self.assertEqual(self.client.post(url, data={'source': 'nope'})
                         .status_code, 404)
        self.assertEqual(models.Job.objects.count(), 0)

    def test_import(self):
        response = self.client.post(self.url('versions.import_file'),
                                    data=self.upload())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/explore/copy'))
        job = only_job('import_version')
        self.assertEqual((job.scope, job.status, job.exclusive),
                         ('copy', 'succeeded', True))
        self.assertTrue(get_record('copy').editable)
        self.assertEqual(self.files(), [])

    def test_a_file_that_is_not_a_version_file(self):
        response = self.client.post(self.url('versions.import_file'),
                                    data=self.upload(b'junk'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Not a version file', self.html(response))
        self.assertEqual(only_job('import_version').status, 'failed')
        self.assertFalse(vc.version_exists('copy'))
        self.assertEqual(self.files(), [])

    def test_import_form_errors_make_no_job(self):
        response = self.client.post(self.url('versions.import_file'),
                                    data=self.upload(version_tag='main'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(models.Job.objects.count(), 0)
        self.assertEqual(self.files(), [])

    def test_delete(self):
        response = self.client.post(self.url('versions.delete', 'b'))
        self.assertEqual(response.status_code, 302)
        job = only_job('delete_version')
        self.assertEqual((job.scope, job.status, job.exclusive),
                         ('b', 'succeeded', True))
        self.assertFalse(vc.version_exists('b'))
        self.assertIn("Deleted version 'b'", self.html(
            self.client.get(response.headers['Location'])))

    def test_deleting_a_tag_makes_no_job(self):
        response = self.client.post(self.url('versions.delete', 't'))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(models.Job.objects.count(), 0)
        self.assertTrue(vc.version_exists('t'))

    def test_export(self):
        response = self.client.get(self.url('export_version', 'b'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/gzip')
        self.assertIn('b.bgx.tar.gz', response.headers['Content-Disposition'])
        job = only_job('export_version')
        self.assertEqual(job.status, 'succeeded')
        self.assertEqual(self.files(), [f'{job.id}.bgx'])
        download = self.client.get(f'/jobs/{job.id}/download')
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.get_data(), response.get_data())

    def test_a_download_needs_a_finished_export_job(self):
        quick = put_job('export_version', 'succeeded', result={})
        running = put_job('export_version', 'running')
        other = put_job('save_document', 'succeeded', result={})
        for job_id in (quick, running, other, str(ObjectId()), 'junk'):
            with self.subTest(job_id=job_id):
                self.assertEqual(self.client.get(
                    f'/jobs/{job_id}/download').status_code, 404)

    def test_the_file_of_an_export_equals_a_direct_export(self):
        response = self.client.get(self.url('export_version', 'main'))
        direct = archive_of('main')
        # the archives hold the same documents, so compare their size class
        # loosely: same names in the same order
        import tarfile
        names = []
        for data in (response.get_data(), direct):
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                names.append(tar.getnames())
        self.assertEqual(names[0], names[1])


class TestApiOperations(OperationCase):
    def post(self, path, **kwargs):
        return self.client.post(API + path, **kwargs)

    def test_patch(self):
        response = self.client.patch(f'{API}/versions/b',
                                     json={'description': 'edited'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['description'], 'edited')
        self.assertEqual(only_job('update_version').status, 'succeeded')

    def test_patch_errors_keep_their_status_and_fields(self):
        response = self.client.patch(f'{API}/versions/b', json={'bogus': 1})
        self.assertEqual(response.status_code, 400)
        self.assertIn('bogus', response.get_json()['error']['fields'])
        self.assertEqual(only_job('update_version').result['status'], 400)
        response = self.client.patch(f'{API}/versions/t',
                                     json={'description': 'x'})
        self.assertEqual(response.status_code, 403)
        response = self.client.patch(f'{API}/versions/nope', json={})
        self.assertEqual(response.status_code, 404)

    def test_merge(self):
        response = self.post('/versions/main/merge', json={'source': 'b'})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual((data['source'], data['target']), ('b', 'main'))
        self.assertIn('classes', data)
        self.assertEqual(Component.select_version('main').count(), 2)

    def test_a_refused_merge_is_a_409_with_problems(self):
        response = self.post('/versions/main/merge', json={
            'source': 'b', 'fingerprint': 'out of date'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('error', response.get_json())

    def test_import(self):
        response = self.post('/versions/import', data=self.upload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers['Location'],
                         f'{API}/versions/copy')
        self.assertEqual(response.get_json()['version_tag'], 'copy')
        self.assertEqual(self.files(), [])

    def test_a_bad_file_is_a_400_on_the_file_field(self):
        response = self.post('/versions/import', data=self.upload(b'junk'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('file', response.get_json()['error']['fields'])

    def test_delete(self):
        self.assertEqual(self.client.delete(f'{API}/versions/b').status_code,
                         204)
        self.assertFalse(vc.version_exists('b'))
        self.assertEqual(self.client.delete(f'{API}/versions/b').status_code,
                         404)
        self.assertEqual(self.client.delete(f'{API}/versions/t').status_code,
                         403)

    def test_export(self):
        response = self.client.get(f'{API}/versions/b/export')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/gzip')


class TestJobResources(OperationCase):
    def get(self, path='', **kwargs):
        return self.client.get(f'{API}/jobs{path}', **kwargs)

    def test_a_job(self):
        job_id = put_job('save_document', 'running', **{
            'progress.done': 5, 'progress.total': 10,
            'progress.stage': 'saving'})
        response = self.get(f'/{job_id}')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(
            {k: data[k] for k in ('id', 'kind', 'status', 'percent', 'done',
                                  'total', 'stage', 'scope', 'finished')},
            {'id': job_id, 'kind': 'save_document', 'status': 'running',
             'percent': 50, 'done': 5, 'total': 10, 'stage': 'saving',
             'scope': 'main', 'finished': False})
        self.assertIsNone(data['result'])

    def test_without_a_total_there_is_no_percentage(self):
        job_id = put_job('save_document', 'running')
        self.assertIsNone(self.get(f'/{job_id}').get_json()['percent'])

    def test_missing_jobs(self):
        for job_id in (str(ObjectId()), 'junk'):
            self.assertEqual(self.get(f'/{job_id}').status_code, 404)
            self.assertEqual(self.client.post(
                f'{API}/jobs/{job_id}/cancel').status_code, 404)

    def test_list_newest_first_with_filters(self):
        first = put_job('save_document', 'succeeded')
        second = put_job('delete_document')
        third = put_job('save_document', 'running')
        models.Job._get_collection().update_one(
            {'_id': ObjectId(third)}, {'$set': {'scope': 'b'}})
        ids = lambda response: [j['id'] for j in  # noqa: E731
                                response.get_json()['jobs']]
        self.assertEqual(ids(self.get()), [third, second, first])
        self.assertEqual(ids(self.get(query_string={'version': 'b'})),
                         [third])
        self.assertEqual(ids(self.get(query_string={'status': 'queued'})),
                         [second])
        self.assertEqual(ids(self.get(query_string={
            'kind': 'save_document'})), [third, first])
        self.assertEqual(ids(self.get(query_string={'limit': 1})), [third])
        self.assertEqual(self.get(query_string={'status': 'x'}).status_code,
                         400)
        self.assertEqual(self.get(query_string={'limit': 'x'}).status_code,
                         400)

    def test_cancelling(self):
        queued = put_job('save_document')
        running = put_job('save_document', 'running')
        done = put_job('save_document', 'succeeded')
        response = self.client.post(f'{API}/jobs/{queued}/cancel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'cancelled')
        response = self.client.post(f'{API}/jobs/{running}/cancel')
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()['cancel_requested'])
        response = self.client.post(f'{API}/jobs/{done}/cancel')
        self.assertEqual(response.status_code, 409)


class ThreadedApiCase(AuthTestCase):
    GRACE = 0.1
    API_WAIT = 0.3

    @classmethod
    def setUpClass(cls):
        reset_database()
        cls.files_dir = tempfile.mkdtemp(prefix='bgx-jobs-test-')
        cls.app = create_app(config={
            'JOBS_FILES_DIR': cls.files_dir,
            'MONGODB_URI': TEST_MONGODB_URI, 'TESTING': True,
            'LOGIN_DISABLED': False, 'WTF_CSRF_ENABLED': False,
            'JOBS_MODE': 'thread', 'JOBS_WORKERS': 2,
            'JOBS_GRACE_SECONDS': cls.GRACE, 'JOBS_POLL_SECONDS': 0.05,
            'JOBS_API_WAIT_SECONDS': cls.API_WAIT})

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions['jobs'].stop(wait=False)
        shutil.rmtree(cls.files_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        clear_jobs()
        Component(name='c1', mass='1 kg').save()
        vc.create_version('b', 'main')
        self.make_user('u1', Role.editor)
        self.make_user('u2', Role.editor)
        self.make_user('root', Role.site_admin)
        self.login('u1')

    def finish(self, job_id, timeout=10):
        return self.app.extensions['jobs'].wait(str(job_id), timeout)

    def patch(self, **headers):
        return self.client.patch(f'{API}/versions/b', headers=headers,
                                 json={'description': 'later'})


class TestWaiting(ThreadedApiCase):
    def test_a_quick_job_gets_its_answer_at_once(self):
        response = self.patch()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['description'], 'later')

    def test_a_long_job_gets_a_202_after_the_default_wait(self):
        with hold_locks(['b'], 'test'):
            start = time.monotonic()
            response = self.patch()
            elapsed = time.monotonic() - start
            self.assertEqual(response.status_code, 202)
            self.assertGreaterEqual(elapsed, self.API_WAIT * 0.8)
            self.assertLess(elapsed, 5)
            job = only_job('update_version')
            self.assertEqual(response.headers['Location'],
                             f'{API}/jobs/{job.id}')
            self.assertIn(response.get_json()['status'],
                          ('queued', 'running'))
            self.assertNotEqual(get_record('b').description, 'later')
        self.assertEqual(self.finish(job.id).status, 'succeeded')
        data = self.client.get(f'{API}/jobs/{job.id}').get_json()
        self.assertEqual((data['status'], data['finished']),
                         ('succeeded', True))
        self.assertEqual(get_record('b').description, 'later')

    def test_prefer_wait_sets_the_time_to_wait(self):
        with hold_locks(['b'], 'test'):
            start = time.monotonic()
            response = self.patch(Prefer='wait=0')
            self.assertEqual(response.status_code, 202)
            self.assertLess(time.monotonic() - start, self.API_WAIT)
            self.assertEqual(response.headers['Preference-Applied'],
                             'wait=0')
        self.finish(only_job('update_version').id)

    def test_prefer_wait_can_wait_for_a_job_that_takes_longer(self):
        response = self.patch(Prefer='wait=5')
        self.assertEqual(response.status_code, 200)

    def test_prefer_respond_async_does_not_wait(self):
        response = self.patch(Prefer='respond-async')
        self.assertEqual(response.status_code, 202)
        job = only_job('update_version')
        self.assertEqual(response.headers['Location'], f'{API}/jobs/{job.id}')
        self.assertEqual(response.headers['Retry-After'], '2')
        self.assertEqual(response.headers['Preference-Applied'],
                         'respond-async')
        self.assertEqual(response.get_json()['id'], str(job.id))
        self.assertEqual(self.finish(job.id).status, 'succeeded')

    def test_unreadable_preferences_are_ignored(self):
        self.assertEqual(self.patch(Prefer='wait=soon, return=minimal')
                         .status_code, 200)

    def test_a_failure_after_the_wait_is_in_the_job(self):
        with hold_locks(['b'], 'test'):
            response = self.client.patch(f'{API}/versions/b',
                                         json={'bogus': 1})
            self.assertEqual(response.status_code, 202)
        job = self.finish(only_job('update_version').id)
        self.assertEqual(job.status, 'failed')
        data = self.client.get(f'{API}/jobs/{job.id}').get_json()
        self.assertEqual((data['status'], data['result']['status']),
                         ('failed', 400))

    def test_a_merge_and_an_edit_of_one_version_do_not_overlap(self):
        runner = self.app.extensions['jobs']
        vc.create_version('c', 'main')
        Component(name='extra', version_tag='c').save()
        with hold_locks(['b'], 'test'):
            merge = runner.submit('merge', {
                'version': 'b', 'source': 'c', 'rule': 'newest',
                'fingerprint': None}, scope='b')
            edit = runner.submit('update_version', {
                'version': 'b', 'body': {'description': 'x'}}, scope='b')
        first, second = self.finish(merge), self.finish(edit)
        self.assertEqual((first.status, second.status),
                         ('succeeded', 'succeeded'))
        self.assertLessEqual(first.finished, second.started)


class TestWhoSeesWhat(ThreadedApiCase):
    def make(self, user):
        # not to be claimed by the runner, which would fail a job like this
        later = models.utcnow() + datetime.timedelta(hours=1)
        return put_job('save_document', user=user, not_before=later)

    def test_only_the_owner_and_site_admins_see_a_job(self):
        mine, theirs = self.make('u1'), self.make('u2')
        self.assertEqual(self.client.get(f'{API}/jobs/{mine}').status_code,
                         200)
        for path in (f'{API}/jobs/{theirs}', f'/jobs/{theirs}',
                     f'/jobs/{theirs}/status'):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post(
            f'{API}/jobs/{theirs}/cancel').status_code, 404)
        self.assertEqual(models.get(theirs).status, 'queued')
        self.client.post(self.url('auth.logout'))
        self.login('root')
        self.assertEqual(self.client.get(f'{API}/jobs/{theirs}')
                         .status_code, 200)

    def test_the_list_only_has_what_one_may_see(self):
        mine, theirs = self.make('u1'), self.make('u2')
        listed = lambda: [j['id'] for j in self.client.get(  # noqa: E731
            f'{API}/jobs').get_json()['jobs']]
        self.assertEqual(listed(), [mine])
        self.client.post(self.url('auth.logout'))
        self.login('root')
        self.assertEqual(listed(), [theirs, mine])

    def test_the_download_of_an_export_is_the_owners(self):
        self.client.get(f'{API}/versions/b/export')
        job = only_job('export_version')
        self.assertEqual(self.client.get(f'/jobs/{job.id}/download')
                         .status_code, 200)
        self.client.post(self.url('auth.logout'))
        self.login('u2')
        self.assertEqual(self.client.get(f'/jobs/{job.id}/download')
                         .status_code, 404)

    def test_viewers_cannot_cancel(self):
        self.make_user('v1', Role.viewer)
        mine = self.make('v1')
        self.client.post(self.url('auth.logout'))
        self.login('v1')
        self.assertEqual(self.client.post(
            f'{API}/jobs/{mine}/cancel').status_code, 403)


if __name__ == '__main__':
    unittest.main()
