""" Looking after jobs: the maintenance functions, the admin page and the
`bgexplorer-jobs` command """
import datetime
import os
import shutil
import tempfile
import unittest
import mongoengine
from click.testing import CliRunner
from bgexplorer import jobs
from bgexplorer.cli import jobs_cli
from bgexplorer.jobs import models, maintenance
from bgexplorer.jobs.context import JobContext
from bgexplorer.models.users import Role
from tests.dbutil import TEST_MONGODB_URI, check_test_db
from tests.test_auth import AuthTestCase
from tests.test_jobs_app import clear_jobs, put_job


@jobs.job_kind('admin_serial', serial=True, label='Serial test')
def _serial(params):
    return {}


def finished_job(status='succeeded', **fields):
    return put_job('app_quick', status, **fields)


class DatabaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_test_db()

    def setUp(self):
        mongoengine.connect(host=TEST_MONGODB_URI)
        self.addCleanup(mongoengine.disconnect)
        clear_jobs()


class TestMaintenance(DatabaseCase):
    def test_cancel_a_queued_or_running_job(self):
        queued, running = put_job(), put_job(status='running')
        self.assertEqual(maintenance.cancel_job(queued), 'cancelled')
        self.assertEqual(models.get(queued).status, 'cancelled')
        self.assertEqual(maintenance.cancel_job(running), 'requested')
        self.assertTrue(models.get(running).cancel_requested)
        self.assertEqual(models.get(running).status, 'running')
        self.assertEqual(maintenance.cancel_job(finished_job()), 'finished')
        self.assertIsNone(maintenance.cancel_job('nonsense'))

    def test_abandon_a_running_job(self):
        job_id = models.submit('admin_serial', scope='s')
        models.submit('admin_serial', scope='s')
        job = models.claim('w1', 60)
        self.assertEqual(str(job.id), job_id)
        self.assertIsNone(models.claim('w2', 60))
        self.assertTrue(maintenance.abandon_job(job_id, "It hung"))
        job = models.get(job_id)
        self.assertEqual((job.status, job.error), ('abandoned', "It hung"))
        self.assertIsNone(job.lease)
        self.assertIsNone(job.slot)
        self.assertIsNotNone(job.finished)
        self.assertIsNotNone(job.expires_at)
        # the scope is free for the next job
        self.assertIsNotNone(models.claim('w2', 60))

    def test_the_abandoned_job_finds_out(self):
        job_id = models.submit('admin_serial', scope='s')
        models.claim('w1', 60)
        context = JobContext(job_id, 'w1')
        context.check_cancelled()
        maintenance.abandon_job(job_id)
        with self.assertRaises(jobs.JobLost):
            context.progress(1, 2)
        self.assertEqual(models.get(job_id).status, 'abandoned')

    def test_only_a_running_job_is_abandoned(self):
        for status in ('queued', 'succeeded', 'failed', 'cancelled'):
            job_id = put_job(status=status)
            self.assertFalse(maintenance.abandon_job(job_id), status)
            self.assertEqual(models.get(job_id).status, status)
        self.assertFalse(maintenance.abandon_job('nonsense'))

    def test_clear_the_finished_jobs(self):
        keep = [put_job(), put_job(status='running')]
        gone = [finished_job(s) for s in ('succeeded', 'failed', 'cancelled',
                                          'abandoned')]
        self.assertEqual(sorted(maintenance.clear_jobs()), sorted(gone))
        self.assertEqual(sorted(str(j.id) for j in models.Job.objects),
                         sorted(keep))

    def test_clear_everything_but_running_jobs(self):
        queued, running = put_job(), put_job(status='running')
        done = finished_job()
        self.assertEqual(sorted(maintenance.clear_jobs(finished_only=False)),
                         sorted([queued, done]))
        self.assertEqual([str(j.id) for j in models.Job.objects], [running])

    def test_clear_jobs_older_than(self):
        now = models.utcnow()
        old = finished_job(finished=now - datetime.timedelta(days=3))
        new = finished_job(finished=now - datetime.timedelta(hours=1))
        self.assertEqual(maintenance.clear_jobs(
            older_than=datetime.timedelta(days=1)), [old])
        self.assertEqual([str(j.id) for j in models.Job.objects], [new])

    def test_clearing_removes_the_files_of_the_jobs(self):
        folder = tempfile.mkdtemp(prefix='bgx-jobs-admin-')
        self.addCleanup(shutil.rmtree, folder, True)
        gone, kept = finished_job(), put_job()
        for name in (f'{gone}.bgx', f'{kept}.bgx', 'other.bgx'):
            open(os.path.join(folder, name), 'w').close()
        maintenance.clear_jobs(files_dir=folder)
        self.assertEqual(sorted(os.listdir(folder)),
                         sorted([f'{kept}.bgx', 'other.bgx']))
        maintenance.clear_jobs(files_dir=os.path.join(folder, 'missing'))

    def test_the_default_files_folder(self):
        self.assertTrue(os.path.isabs(maintenance.default_files_dir()))


class TestAdminJobs(AuthTestCase):
    def setUp(self):
        super().setUp()
        clear_jobs()
        self.make_user('root', Role.site_admin)
        self.make_user('ed', Role.editor)
        self.login('root')

    def page(self):
        return self.html(self.client.get(self.url('admin.maintenance_page')))

    def post(self, endpoint, job_id=None, **data):
        values = {} if job_id is None else {'job_id': job_id}
        return self.client.post(self.url(endpoint, **values), data=data)

    def test_the_table_lists_the_jobs(self):
        queued = put_job(user='ed')
        running = put_job('app_hold', 'running', user='root',
                          **{'progress.done': 1, 'progress.total': 4})
        done = finished_job(user='ed')
        html = self.page()
        self.assertIn('id="jobs"', html)
        for job_id in (queued, running, done):
            self.assertIn(f'/jobs/{job_id}', html)
        self.assertIn('Held test', html)
        self.assertIn('25%', html)

    def test_the_table_when_there_are_no_jobs(self):
        html = self.page()
        self.assertIn('id="jobs"', html)
        self.assertIn('No jobs', html)

    def test_cancel_a_queued_job(self):
        job_id = put_job()
        response = self.post('admin.cancel_job', job_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(models.get(job_id).status, 'cancelled')

    def test_cancel_a_running_job_asks_it_to_stop(self):
        job_id = put_job(status='running')
        self.post('admin.cancel_job', job_id)
        self.assertTrue(models.get(job_id).cancel_requested)

    def test_abandon_a_running_job(self):
        job_id = put_job(status='running')
        response = self.post('admin.abandon_job', job_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(models.get(job_id).status, 'abandoned')
        self.assertIn('abandoned', self.page())

    def test_a_job_that_is_not_running_is_not_abandoned(self):
        job_id = put_job()
        response = self.post('admin.abandon_job', job_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(models.get(job_id).status, 'queued')

    def test_an_unknown_job_is_a_404(self):
        for endpoint in ('admin.cancel_job', 'admin.abandon_job'):
            self.assertEqual(self.post(endpoint, 'f' * 24).status_code, 404)

    def test_clear_the_finished_jobs(self):
        queued, done = put_job(), finished_job()
        response = self.post('admin.clear_jobs')
        self.assertEqual(response.status_code, 302)
        self.assertEqual([str(j.id) for j in models.Job.objects], [queued])
        self.assertIsNone(models.get(done))

    def test_only_a_site_admin_may_use_them(self):
        job_id = put_job(user='ed', status='running')
        self.client = self.app.test_client()
        self.login('ed')
        for endpoint, values in (('admin.cancel_job', {'job_id': job_id}),
                                 ('admin.abandon_job', {'job_id': job_id}),
                                 ('admin.clear_jobs', {})):
            response = self.client.post(self.url(endpoint, **values))
            self.assertEqual(response.status_code, 403, endpoint)
        self.assertEqual(models.get(job_id).status, 'running')
        self.assertEqual(
            self.client.get(self.url('admin.maintenance_page')).status_code,
            403)


class TestJobsCli(DatabaseCase):
    def invoke(self, *args):
        env = {'FLASK_SECRET_KEY': None, 'BGEXPLORER_CONFIG': None}
        return CliRunner().invoke(jobs_cli, ['--uri', TEST_MONGODB_URI, *args],
                                  env=env)

    def test_list(self):
        queued = put_job(user='ed')
        done = finished_job('failed', error='It broke')
        result = self.invoke('list')
        self.assertEqual(result.exit_code, 0, result.output)
        lines = result.output.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(queued, result.output)
        self.assertIn('queued', result.output)
        self.assertIn('ed', result.output)
        self.assertIn('failed', result.output)
        self.assertIn(done, result.output)

    def test_list_filters(self):
        put_job()
        done = finished_job()
        result = self.invoke('list', '--status', 'succeeded')
        self.assertEqual(result.output.count('\n'), 1)
        self.assertIn(done, result.output)
        self.assertNotEqual(self.invoke('list', '--status', 'bogus')
                            .exit_code, 0)
        self.assertEqual(self.invoke('list', '--kind', 'nothing').output, '')

    def test_cancel(self):
        queued, running = put_job(), put_job(status='running')
        result = self.invoke('cancel', queued)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(models.get(queued).status, 'cancelled')
        self.assertIn('cancelled', result.output)
        result = self.invoke('cancel', running)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(models.get(running).cancel_requested)
        result = self.invoke('cancel', finished_job())
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('already', result.output)
        result = self.invoke('cancel', 'f' * 24)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('No job', result.output)

    def test_abandon(self):
        running = put_job(status='running')
        result = self.invoke('abandon', running)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(models.get(running).status, 'abandoned')
        result = self.invoke('abandon', put_job())
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn('not running', result.output)

    def test_clear(self):
        queued, done = put_job(), finished_job()
        result = self.invoke('clear')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('Removed 1 job', result.output)
        self.assertEqual([str(j.id) for j in models.Job.objects], [queued])
        self.assertEqual(self.invoke('clear', '--all').exit_code, 0)
        self.assertEqual(models.Job.objects.count(), 0)
        self.assertIsNone(models.get(done))

    def test_clear_removes_export_files(self):
        folder = tempfile.mkdtemp(prefix='bgx-jobs-admin-')
        self.addCleanup(shutil.rmtree, folder, True)
        done = finished_job()
        open(os.path.join(folder, f'{done}.bgx'), 'w').close()
        result = self.invoke('clear', '--files-dir', folder)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(os.listdir(folder), [])


if __name__ == '__main__':
    unittest.main()
