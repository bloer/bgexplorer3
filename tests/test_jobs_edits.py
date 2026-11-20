""" Saving, deleting and cloning documents, and saving version settings, as
background jobs """
import unittest
from bson import ObjectId
from bgexplorer.application.app import create_app
from bgexplorer.jobs import models
from bgexplorer.models.component import Component
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.settings import get_settings
from bgexplorer.models.users import Role
from bgexplorer.versioning import control as vc
from bgexplorer.versioning.locks import hold_locks
from bgexplorer.versioning.record import get_record
from tests.dbutil import TEST_MONGODB_URI
from tests.test_app import reset_database
from tests.test_app_components import AppTestCase
from tests.test_auth import AuthTestCase
from tests.test_jobs_app import clear_jobs


def only_job(kind=None) -> models.Job:
    jobs = models.Job.objects(kind=kind) if kind else models.Job.objects
    return jobs.get()


class TestEditsAreJobs(AppTestCase):
    """ In eager mode, a job is done by the time the request redirects, so
    every page behaves as it always did """
    def setUp(self):
        super().setUp()
        clear_jobs()
        vc.create_version('main')
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='K40', rate='1 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='1 kg', specs=[self.e1]).save()
        vc.create_version('b', 'main')

    def test_edit(self):
        url = self.url('component.edit', 'b', object=self.c1)
        response = self.client.post(url, data={'description': 'edited'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'],
                         self.url('component.view', 'b', object=self.c1))
        job = only_job()
        self.assertEqual((job.kind, job.scope, job.status),
                         ('save_document', 'b', 'succeeded'))
        self.assertEqual(job.origin, url)
        self.assertEqual(Component.select_version('b').get(name='c1')
                         .description, 'edited')
        self.assertIn('Successfully saved c1',
                      self.html(self.client.get(response.headers['Location'])))

    def test_new(self):
        url = self.url('component.edit', 'b')
        response = self.client.post(url, data={'name': 'c2', 'mass': '2 kg'})
        self.assertEqual(response.status_code, 302)
        c2 = Component.select_version('b').get(name='c2')
        self.assertEqual(c2.version_tags, ['b'])
        self.assertEqual(response.headers['Location'],
                         self.url('component.view', 'b', object=c2))

    def test_new_owned_spec(self):
        url = self.url('emissionspec.edit', 'b', type='emissionspec',
                       owner=self.c1.original_id, confidence_rating=3,
                       location_mode='suffix', location=' surface')
        response = self.client.post(url, data={
            'name': 'c1 own', '_listfields': 'sources', 'sources.id': '',
            'sources.name': 'Co60', 'sources.rate': '1 mBq/kg'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'],
                         self.url('component.view', 'b', object=self.c1))
        c1 = Component.select_version('b').get(name='c1')
        entry = c1.find_spec_entry(c1.specs[1])
        self.assertEqual((entry.confidence_rating, entry.location,
                          entry.location_mode.value),
                         (3, ' surface', 'suffix'))
        self.assertEqual(only_job().params['owner'], str(c1.original_id))

    def test_form_errors_make_no_job(self):
        url = self.url('component.edit', 'b', object=self.c1)
        response = self.client.post(url, data={'mass': 'heavy'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('id="toperror"', self.html(response))
        # a value that parses but is not valid
        response = self.client.post(url, data={'confidence_rating': '9'})
        self.assertEqual(response.status_code, 400)
        HitEfficiency(source='K40', location='c1',
                      scalars=dict(v1='0.1 dru/mBq')).save()
        vc.create_version('e', 'main')
        response = self.client.post(
            self.url('edit_settings', 'e'),
            data={'hiteffdbconfig.display_scalars[v1].display_unit': 'kg',
                  'description': 'x'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(models.Job.objects.count(), 0)

    def test_bad_owned_spec_settings_make_no_job(self):
        url = self.url('emissionspec.edit', 'b', type='emissionspec',
                       owner=self.c1.original_id, confidence_rating='high')
        self.assertEqual(self.client.post(url, data={'name': 'x'})
                         .status_code, 400)
        self.assertEqual(models.Job.objects.count(), 0)

    def test_read_only_versions_make_no_job(self):
        vc.create_tag('t', 'main')
        url = self.url('component.edit', 't', object=self.c1)
        self.assertEqual(self.client.post(url, data={'description': 'x'})
                         .status_code, 403)
        self.assertEqual(models.Job.objects.count(), 0)

    def test_delete(self):
        url = self.url('component.delete', 'b', object=self.c1)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'],
                         self.url('component.overview', 'b'))
        self.assertEqual(only_job().kind, 'delete_document')
        self.assertEqual(Component.select_version('b').count(), 0)
        self.assertEqual(Component.select_version('main').count(), 1)
        self.assertIn('Deleted c1 from b',
                      self.html(self.client.get(response.headers['Location'])))

    def test_clone(self):
        url = self.url('component.clone', 'b', object=self.c1)
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        copy = Component.select_version('b').get(name='c1 (copy)')
        self.assertEqual(response.headers['Location'],
                         self.url('component.edit', 'b', object=copy))
        self.assertEqual(only_job().kind, 'clone_document')
        self.assertIn('Created c1 (copy) as a copy of c1',
                      self.html(self.client.get(response.headers['Location'])))

    def test_a_document_deleted_before_the_job_runs_fails_the_job(self):
        job_id = self.app.extensions['jobs'].submit(
            'save_document',
            {'cls': 'Component', 'version': 'b', 'objid': str(ObjectId()),
             'type': None, 'owner': None, 'settings': {}, 'form': {}},
            scope='b')
        job = models.get(job_id)
        self.assertEqual(job.status, 'failed')
        self.assertIn('No Component with id', job.error)

    def test_settings(self):
        url = self.url('edit_settings', 'b')
        response = self.client.post(url, data={'description': 'edited'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'],
                         self.url('overview', 'b'))
        self.assertEqual(only_job().kind, 'save_settings')
        self.assertEqual(get_record('b').description, 'edited')
        self.assertIn('Settings saved',
                      self.html(self.client.get(response.headers['Location'])))

    def test_settings_report_progress_over_the_hit_efficiencies(self):
        for location in ('c1', 'c2', 'c3'):
            HitEfficiency(source='K40', location=location,
                          scalars=dict(v1='0.1 dru/mBq')).save()
        vc.create_version('h', 'main')
        response = self.client.post(self.url('edit_settings', 'h'), data={
            'description': 'with ROIs',
            '_listfields': ['hiteffdbconfig.rois'],
            'hiteffdbconfig.rois.display_name': ['roi1'],
            'hiteffdbconfig.rois.link_spectrum': ['s1'],
            'hiteffdbconfig.rois.start': ['1 keV'],
            'hiteffdbconfig.rois.stop': ['10 keV'],
            'hiteffdbconfig.rois.mode': ['integrate'],
            'hiteffdbconfig.rois.binwidths': ['true'],
            'hiteffdbconfig.rois.hide': ['false']})
        self.assertEqual(response.status_code, 302)
        progress = only_job('save_settings').progress
        self.assertEqual((progress.stage, progress.done, progress.total),
                         ('hit efficiencies', 3, 3))


    def test_settings_roi_exclusions(self):
        vc.create_version('x', 'main')
        url = self.url('edit_settings', 'x')
        data = {
            'description': 'with exclusions',
            '_listfields': ['hiteffdbconfig.rois'],
            'hiteffdbconfig.rois.display_name': ['roi1'],
            'hiteffdbconfig.rois.link_spectrum': ['s1'],
            'hiteffdbconfig.rois.start': ['1 keV'],
            'hiteffdbconfig.rois.stop': ['10 keV'],
            'hiteffdbconfig.rois.exclude': ['abc'],
            'hiteffdbconfig.rois.mode': ['integrate'],
            'hiteffdbconfig.rois.binwidths': ['true'],
            'hiteffdbconfig.rois.hide': ['false']}
        response = self.client.post(url, data=data)
        self.assertEqual(response.status_code, 400)
        page = self.html(response)
        self.assertIn('as a range', page)
        self.assertIn('is-invalid', page)
        data['hiteffdbconfig.rois.exclude'] = ['2-4, 6-7 keV']
        response = self.client.post(url, data=data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            get_settings('x').hiteffdbconfig.rois[0].exclude, '2-4, 6-7 keV')
        self.assertIn('2-4, 6-7 keV',
                      self.html(self.client.get(url)))


def add_spectrum(tag, name='s1', location='c1'):
    """ A hit efficiency in `tag` with a spectrum of 4 bins from 0 to 4 keV """
    import numpy as np
    from bgexplorer.models.asymmetric import AsymmetricUncertainty
    from bgexplorer.models.common import units
    from bgexplorer.models.histogram import Histogram
    hist = Histogram(AsymmetricUncertainty.fromcounts(np.ones(4))
                     * units('dru/mBq'), np.arange(5.) * units.keV)
    return HitEfficiency(source='K40', location=location, version_tag=tag,
                         spectra={name: hist}).save()


def binning(tag, name='s1'):
    config = get_settings(tag, create=False).hiteffdbconfig
    return config.display_spectra[name].bin_edges


class TestSpectrumBinning(AppTestCase):
    def setUp(self):
        super().setUp()
        clear_jobs()
        vc.create_version('main')
        vc.create_version('b', 'main')
        self.hiteff = add_spectrum('b')
        self.settings_url = self.url('edit_settings', 'b')
        self.reset = self.url('versions.reset_binning', 'b')

    def test_the_form_shows_and_keeps_the_binning(self):
        html = self.html(self.client.get(self.settings_url))
        self.assertIn('4 bins from 0 to 4 keV', html)
        self.assertIn('form="resetbinning"', html)
        key = 'hiteffdbconfig.display_spectra[s1]'
        response = self.client.post(self.settings_url, data={
            f'{key}.display_name': 'Spectrum 1', f'{key}.display_unit': '',
            f'{key}.description': '', f'{key}.hide': 'false'})
        self.assertEqual(response.status_code, 302)
        config = get_settings('b', create=False).hiteffdbconfig
        self.assertEqual(config.display_spectra['s1'].display_name,
                         'Spectrum 1')
        self.assertEqual(binning('b').m.tolist(), [0, 1, 2, 3, 4])

    def test_reset_is_refused_while_spectra_exist(self):
        response = self.client.post(self.reset, data={'key': 's1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], self.settings_url)
        job = only_job('reset_binning')
        self.assertEqual((job.scope, job.status, job.params['key']),
                         ('b', 'failed', 's1'))
        self.assertIn('Delete those spectra', job.error)
        self.assertIsNotNone(binning('b'))

    def test_reset(self):
        self.hiteff.remove_spectrum('s1')
        response = self.client.post(self.reset, data={'key': 's1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(only_job('reset_binning').status, 'succeeded')
        self.assertIsNone(binning('b'))
        self.assertIn('set by the next spectrum',
                      self.html(self.client.get(self.settings_url)))
        # the next spectrum sets the binning again
        add_spectrum('b', location='c2')
        self.assertIsNotNone(binning('b'))

    def test_a_missing_key_is_refused(self):
        self.assertEqual(self.client.post(self.reset).status_code, 400)
        self.assertEqual(models.Job.objects.count(), 0)


class TestResetBindingPermission(AuthTestCase):
    def test_only_admins_reset_the_binning(self):
        vc.create_version('b', 'main')
        add_spectrum('b')
        self.make_user('ed', Role.editor)
        self.make_user('boss', Role.admin)
        url = self.url('versions.reset_binning', 'b')
        self.login('ed')
        html = self.html(self.client.get(self.url('edit_settings', 'b')))
        self.assertIn('4 bins from 0 to 4 keV', html)
        self.assertNotIn('id="resetbinning"', html)
        self.assertEqual(self.client.post(url, data={'key': 's1'})
                         .status_code, 403)
        self.client.post(self.url('auth.logout'))
        self.login('boss')
        html = self.html(self.client.get(self.url('edit_settings', 'b')))
        self.assertIn('id="resetbinning"', html)


class ThreadedEditCase(AuthTestCase):
    """ Logins and a pool of worker threads, with a short grace period """
    @classmethod
    def setUpClass(cls):
        reset_database()
        cls.app = create_app(config={
            'MONGODB_URI': TEST_MONGODB_URI, 'TESTING': True,
            'LOGIN_DISABLED': False, 'WTF_CSRF_ENABLED': False,
            'JOBS_MODE': 'thread', 'JOBS_WORKERS': 2,
            'JOBS_GRACE_SECONDS': 0.1, 'JOBS_POLL_SECONDS': 0.05})

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions['jobs'].stop(wait=False)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        clear_jobs()
        self.c1 = Component(name='c1', mass='1 kg').save()
        vc.create_version('b', 'main')
        self.make_user('u1', Role.editor)
        self.login('u1')

    def finish(self, job_id, timeout=10):
        return self.app.extensions['jobs'].wait(str(job_id), timeout)


class TestEditsInTheBackground(ThreadedEditCase):
    def test_the_requesting_user_is_recorded(self):
        url = self.url('component.edit', 'b', object=self.c1)
        self.client.post(url, data={'description': 'by u1'})
        job = self.finish(only_job().id)
        self.assertEqual((job.status, job.user), ('succeeded', 'u1'))
        c1 = Component.select_version('b').get(name='c1')
        self.assertEqual((c1.description, c1.enteredby), ('by u1', 'u1'))

    def test_an_edit_waits_for_a_locked_version(self):
        url = self.url('component.edit', 'b', object=self.c1)
        with hold_locks(['b'], 'test'):
            response = self.client.post(url, data={'description': 'later'})
            job = only_job()
            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers['Location'].startswith(
                f'/jobs/{job.id}?next='), response.headers['Location'])
            self.assertEqual(self.client.get(response.headers['Location'])
                             .status_code, 200)
            self.finish(job.id, 1.5)
            job = only_job()
            self.assertEqual(job.status, 'queued')
            self.assertIn('locked', job.progress.message)
            self.assertIsNone(Component.select_version('b').get(name='c1')
                              .description)
        job = self.finish(job.id)
        self.assertEqual(job.status, 'succeeded')
        self.assertEqual(Component.select_version('b').get(name='c1')
                         .description, 'later')
        # and the status page now goes on to the document
        response = self.client.get(response.headers['Location'])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'],
                         self.url('component.view', 'b', object=self.c1))
        self.assertIn('Successfully saved c1',
                      self.html(self.client.get(response.headers['Location'])))

    def test_a_settings_save_waits_for_a_locked_version(self):
        url = self.url('edit_settings', 'b')
        with hold_locks(['b'], 'test'):
            response = self.client.post(url, data={'description': 'later'})
            job = only_job()
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers['Location'],
                             f"/jobs/{job.id}?next={self.url('overview', 'b')}")
            self.assertNotEqual(get_record('b').description, 'later')
        self.assertEqual(self.finish(job.id).status, 'succeeded')
        self.assertEqual(get_record('b').description, 'later')

    def test_deleting_waits_too(self):
        url = self.url('component.delete', 'b', object=self.c1)
        with hold_locks(['b'], 'test'):
            response = self.client.post(url)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(Component.select_version('b').count(), 1)
        self.assertEqual(self.finish(only_job().id).status, 'succeeded')
        self.assertEqual(Component.select_version('b').count(), 0)

    def test_form_errors_are_shown_at_once(self):
        url = self.url('component.edit', 'b', object=self.c1)
        response = self.client.post(url, data={'mass': 'heavy'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(models.Job.objects.count(), 0)


if __name__ == '__main__':
    unittest.main()
