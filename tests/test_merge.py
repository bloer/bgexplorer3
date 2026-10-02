""" Merging one version into another """
import time
import unittest
from unittest import mock
from mongoengine import disconnect
from bgexplorer.models import versioncontrol as vc
from bgexplorer.models.versioncontrol import (_versioned_classes, MergeRule,
                                              MergeError, plan_merge,
                                              merge_version, version_exists)
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.sourceterm import SourceTerm
from bgexplorer.models.settings import (VersionSettings, get_settings,
                                        acquire_lock)
from bgexplorer.models.history import VersionEvent, EventAction
from bgexplorer.models.maintenance import rebuild_sourceterms
from bgexplorer.models.versiondiff import settings_content
from tests.dbutil import connect_test_db


def edit(name, tag, **changes):
    """ Change a component in `tag`. Waits a little first, so modification
    times are distinct
    """
    time.sleep(0.01)
    doc = Component.select_version(tag).get(name=name)
    for key, value in changes.items():
        setattr(doc, key, value)
    return doc.save()


def description(name, tag):
    doc = Component.select_version(tag)(name=name).first()
    return doc.description if doc else 'missing'


def sourceterm_signature(tag):
    return sorted((st.componentName, st.assemblyPathStr, st.source.name,
                   str(st.spec.original_id))
                  for st in SourceTerm.select_version(tag))


def snapshot(tag):
    """ Which copies are in `tag`, and its settings """
    return ({cls.__name__: sorted(cls._get_collection().distinct(
                 '_id', {'version_tags': tag}))
             for cls in _versioned_classes},
            settings_content(tag))


class TestMerge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in [VersionSettings, VersionEvent] + _versioned_classes:
            cls.drop_collection()
        vc.create_version('main')
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='K40', rate='1 mBq/kg')]).save()
        for name in ('c1', 'c2', 'c3', 'c4'):
            Component(name=name, mass='1 kg', specs=[self.e1]).save()
        Assembly(name='top', children=[
            Placement(component=Component.select_version('main')
                      .get(name=name)) for name in ('c1', 'c2')]).save()
        vc.create_version('b', 'main')
        # c1 changed in main, then c2 in both with b newer, c3 only in b
        edit('c1', 'main', description='main')
        edit('c2', 'main', description='main')
        edit('c2', 'b', description='b')
        edit('c3', 'b', description='b')
        Component.select_version('main').get(name='c4').delete()
        Component(name='new', version_tag='b').save()

    def merged(self):
        return {name: description(name, 'main')
                for name in ('c1', 'c2', 'c3', 'c4', 'new')}

    def test_plan(self):
        plan = plan_merge('b', 'main', MergeRule.newest)
        self.assertEqual(plan.problems, [])
        components = plan.classes[0]
        self.assertEqual([i.name for i in components.add], ['c4', 'new'])
        self.assertEqual([i.name for i in components.replace], ['c2', 'c3'])
        self.assertEqual([i.name for i in components.keep], ['c1'])
        self.assertEqual(components.target_only, [])
        self.assertTrue(plan.changes)
        # planning changes nothing
        self.assertEqual(self.merged()['c2'], 'main')

    def test_newest(self):
        merge_version('b', 'main', MergeRule.newest)
        # items deleted from main come back if they're in b: a merge only
        # adds items
        self.assertEqual(self.merged(), {'c1': 'main', 'c2': 'b', 'c3': 'b',
                                         'c4': None, 'new': None})
        # main now shares b's copies
        self.assertEqual(Component.select_version('main').get(name='c2').id,
                         Component.select_version('b').get(name='c2').id)
        # b is unchanged
        self.assertEqual(description('c1', 'b'), None)

    def test_source(self):
        merge_version('b', 'main', MergeRule.source)
        self.assertEqual(self.merged(), {'c1': None, 'c2': 'b', 'c3': 'b',
                                         'c4': None, 'new': None})

    def test_target(self):
        merge_version('b', 'main', MergeRule.target)
        self.assertEqual(self.merged(), {'c1': 'main', 'c2': 'main',
                                         'c3': None, 'c4': None,
                                         'new': None})

    def test_target_only_kept(self):
        Component(name='mainonly', version_tag='main').save()
        merge_version('b', 'main', MergeRule.source)
        self.assertEqual(description('mainonly', 'main'), None)
        self.assertEqual(description('mainonly', 'b'), 'missing')

    def test_settings(self):
        settings = get_settings('b')
        settings.hiteffdbconfig.extra_columns = ['material']
        settings.description = 'b description'
        settings.save()
        merge_version('b', 'main', MergeRule.target)
        self.assertEqual(get_settings('main').hiteffdbconfig.extra_columns,
                         [])
        merge_version('b', 'main', MergeRule.source)
        settings = get_settings('main')
        self.assertEqual(settings.hiteffdbconfig.extra_columns, ['material'])
        # the version's own description and editability are kept
        self.assertIsNone(settings.description)
        self.assertTrue(settings.editable)

    def test_sourceterms_consistent(self):
        # a spec changed in b that main's kept components use
        time.sleep(0.01)
        e1 = EmissionSpec.select_version('b').get(name='e1')
        e1.sources.append(EmissionSource(name='Co60', rate='1 mBq/kg'))
        e1.save()
        merge_version('b', 'main', MergeRule.newest)
        merged = sourceterm_signature('main')
        self.assertIn('Co60', {sig[2] for sig in merged})
        vc.create_version('check', 'main')
        rebuild_sourceterms('check')
        self.assertEqual(merged, sourceterm_signature('check'))

    def test_events_and_cleanup(self):
        versions = {v.version_tag for v in vc.list_versions()}
        merge_version('b', 'main')
        # the backup is gone and the locks released
        self.assertEqual({v.version_tag for v in vc.list_versions()},
                         versions)
        self.assertIsNone(get_settings('main').lock)
        self.assertIsNone(get_settings('b').lock)
        event = VersionEvent.objects.first()
        self.assertEqual((event.action, event.version, event.other_version),
                         (EventAction.merge, 'main', 'b'))
        self.assertEqual(event.details['counts']['Component'],
                         {'add': 2, 'replace': 2, 'keep': 1})
        self.assertIn('2 added', event.message)

    def test_nothing_to_merge(self):
        vc.create_version('c', 'main')
        plan = merge_version('c', 'main')
        self.assertFalse(plan.changes)

    def test_refused(self):
        with self.assertRaises(MergeError):
            plan_merge('main', 'main')
        with self.assertRaises(KeyError):
            plan_merge('nosuch', 'main')
        vc.create_tag('t', 'main')
        self.assertTrue(plan_merge('b', 't').problems)
        with self.assertRaises(MergeError):
            merge_version('b', 't')
        acquire_lock('main', 'something else')
        plan = plan_merge('b', 'main')
        self.assertIn('locked by something else', plan.problems[0])
        before = snapshot('main')
        with self.assertRaises(MergeError):
            merge_version('b', 'main')
        self.assertEqual(snapshot('main'), before)
        # a locked source is refused too
        self.assertTrue(plan_merge('main', 'b').problems)

    def test_stale_fingerprint(self):
        plan = plan_merge('b', 'main')
        edit('c4', 'b', description='changed after planning')
        before = snapshot('main')
        with self.assertRaises(MergeError) as cm:
            merge_version('b', 'main', fingerprint=plan.fingerprint)
        self.assertIn('changed since', str(cm.exception))
        self.assertEqual(snapshot('main'), before)
        merge_version('b', 'main',
                      fingerprint=plan_merge('b', 'main').fingerprint)
        self.assertEqual(description('c4', 'main'), 'changed after planning')

    def test_dangling_refs(self):
        # a reference to something that's in neither version
        e2 = EmissionSpec(name='e2', version_tag='b').save()
        edit('new', 'b', specs=[e2])
        EmissionSpec._get_collection().delete_one({'_id': e2.id})
        plan = plan_merge('b', 'main')
        self.assertIn('in neither version', plan.problems[0])

    def test_rollback(self):
        before = snapshot('main')
        signature = sourceterm_signature('main')
        versions = {v.version_tag for v in vc.list_versions()}
        with mock.patch('bgexplorer.models.maintenance.rebuild_sourceterms',
                        side_effect=RuntimeError('boom')):
            with self.assertRaises(MergeError) as cm:
                merge_version('b', 'main', MergeRule.source)
        self.assertIn('restored', str(cm.exception))
        self.assertEqual(snapshot('main'), before)
        self.assertEqual(sourceterm_signature('main'), signature)
        self.assertEqual({v.version_tag for v in vc.list_versions()},
                         versions)
        self.assertIsNone(get_settings('main').lock)
        self.assertEqual(VersionEvent.objects.first().action,
                         EventAction.merge_rollback)
        # main still works
        edit('c1', 'main', description='after')

    def test_rollback_fails(self):
        with mock.patch('bgexplorer.models.maintenance.rebuild_sourceterms',
                        side_effect=RuntimeError('boom')), \
                mock.patch.object(vc, '_restore',
                                  side_effect=RuntimeError('worse')):
            with self.assertRaises(MergeError) as cm:
                merge_version('b', 'main')
        backups = [v.version_tag for v in vc.list_versions()
                   if v.version_tag.startswith('main.premerge-')]
        self.assertEqual(len(backups), 1)
        self.assertIn(backups[0], str(cm.exception))
        self.assertFalse(get_settings(backups[0]).editable)
        self.assertIsNone(get_settings('main').lock)
