""" Exporting whole versions to files and importing them """
import io
import json
import tarfile
import unittest
from unittest import mock
import numpy as np
from bson import ObjectId, json_util
from mongoengine import disconnect
from bgexplorer.models import versioncontrol as vc
from bgexplorer.models.versioncontrol import _versioned_classes, MergeRule
from bgexplorer.models.versionfile import (export_version, import_version,
                                           read_version_file,
                                           VersionFileError)
from bgexplorer.models.versiondiff import diff_versions
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.assay import Assay
from bgexplorer.models.cosmogenic import (ActivatedMaterial, CosmogenicIsotope,
                                          CosmogenicActivation,
                                          ActivationPeriod)
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.common import units
from bgexplorer.models.fields import InlineAttachment
from bgexplorer.models.sourceterm import SourceTerm
from bgexplorer.models.settings import VersionSettings, get_settings
from bgexplorer.models.history import VersionEvent, EventAction
from tests.dbutil import connect_test_db


def spectrum(counts):
    counts = np.asarray(counts, dtype=float)
    return Histogram(AsymmetricUncertainty.fromcounts(counts)
                     * units('dru/mBq'),
                     np.arange(len(counts) + 1.) * units.keV)


def make_model(tag='src'):
    """ A version with one of most kinds of document """
    vc.create_version(tag)
    settings = get_settings(tag)
    settings.hiteffdbconfig.extra_columns = ['material']
    settings.save()
    cu = ActivatedMaterial(name='Cu', material='copper', version_tag=tag,
                           isotopes=[CosmogenicIsotope(
                               isotope='Co60',
                               activationrate='97 +- 10 1/kg/day')]).save()
    activation = CosmogenicActivation(
        name='Cu activation', material=cu, version_tag=tag,
        periods=[ActivationPeriod(duration='30 day')]).save()
    assay = Assay(name='assay', version_tag=tag, sources=[
        EmissionSource(name='K40', rate='1 +- 0.1 mBq/kg')]).save()
    c1 = Component(name='c1', mass='1 kg', location='c1', version_tag=tag,
                   specs=[assay, activation], attachments=[InlineAttachment(
                       filename='a.txt', data=b'\x00binary\xff')]).save()
    c2 = Component(name='c2', mass='2 kg', version_tag=tag).save()
    EmissionSpec(name='owned', owner=c2, version_tag=tag, sources=[
        EmissionSource(name='Th232', rate='<3 mBq/kg')]).save()
    c2 = Component.select_version(tag).get(name='c2')
    Assembly(name='top', version_tag=tag, children=[
        Placement(component=c1, weight=2), Placement(component=c2)]).save()
    HitEfficiency(source='K40', location='c1', version_tag=tag,
                  scalars=dict(v1='0.1 +- 0.01 dru/mBq'),
                  spectra=dict(s1=spectrum([1, 2, 0, 4]))).save()


def export_bytes(tag='src') -> bytes:
    buf = io.BytesIO()
    export_version(tag, buf)
    return buf.getvalue()


def make_file(members: dict) -> bytes:
    """ A tar.gz with the given {name: bytes} """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def read_members(data: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar}


def sourceterm_signature(tag):
    return sorted((st.componentName, st.assemblyPathStr, st.source.name,
                   str(st.spec.original_id), st.weight)
                  for st in SourceTerm.select_version(tag))


def snapshot():
    """ Everything in the database that an import could change """
    return ({cls.__name__: cls._get_collection().count_documents({})
             for cls in _versioned_classes},
            sorted(v.version_tag for v in vc.list_versions()))


class TestVersionFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in [VersionSettings, VersionEvent] + _versioned_classes:
            cls.drop_collection()
        make_model()

    def test_file_contents(self):
        members = read_members(export_bytes())
        self.assertEqual(sorted(members), [
            'activated_material.jsonl', 'component.jsonl',
            'emission_spec.jsonl', 'hit_efficiency.jsonl', 'manifest.json',
            'settings.json'])
        manifest = json.loads(members['manifest.json'])
        self.assertEqual(manifest['format'], 'bgexplorer-version')
        self.assertEqual(manifest['version_tag'], 'src')
        self.assertEqual(manifest['counts'], {
            'Component': 3, 'EmissionSpec': 3, 'HitEfficiency': 1,
            'ActivatedMaterial': 1})
        doc = json_util.loads(members['component.jsonl'].splitlines()[0])
        self.assertNotIn('_id', doc)
        self.assertNotIn('version_tags', doc)
        self.assertIn('original_id', doc)
        settings = json_util.loads(members['settings.json'])
        self.assertNotIn('version_tag', settings)
        self.assertEqual(settings['hiteffdbconfig']['extra_columns'],
                         ['material'])

    def test_round_trip(self):
        settings = import_version(io.BytesIO(export_bytes()), 'copy')
        self.assertTrue(settings.editable)
        self.assertIn("Imported from 'src'", settings.description)
        self.assertIsNone(settings.lock)
        self.assertEqual(settings.hiteffdbconfig.extra_columns, ['material'])
        for cls in vc._referenced_classes:
            with self.subTest(cls=cls.__name__):
                src = set(cls._get_collection().distinct(
                    'original_id', {'version_tags': 'src'}))
                copy = set(cls._get_collection().distinct(
                    'original_id', {'version_tags': 'copy'}))
                self.assertEqual(src, copy)
                # new copies, not shared ones
                self.assertEqual(cls._get_collection().count_documents(
                    {'version_tags': {'$all': ['src', 'copy']}}), 0)
        comparison = diff_versions('src', 'copy')
        for result in comparison.classes:
            self.assertEqual((result.differ, result.only_left,
                              result.only_right), ([], [], []))
        self.assertEqual(comparison.settings, [])
        self.assertEqual(sourceterm_signature('copy'),
                         sourceterm_signature('src'))
        # the documents work as usual
        c1 = Component.select_version('copy').get(name='c1')
        self.assertEqual([s.name for s in c1.specs],
                         ['assay', 'Cu activation'])
        self.assertEqual(c1.attachments[0].data, b'\x00binary\xff')
        he = HitEfficiency.select_version('copy').get()
        src_he = HitEfficiency.select_version('src').get()
        self.assertEqual(str(he.spectra['s1'].integrate()),
                         str(src_he.spectra['s1'].integrate()))
        c1.mass = '5 kg'
        c1.save()
        self.assertEqual(Component.select_version('src').get(name='c1')
                         .mass.m, 1)
        # merging the copy back changes only what was edited
        plan = vc.plan_merge('copy', 'src', MergeRule.source)
        self.assertEqual([[i.name for i in c.replace] for c in plan.classes],
                         [['c1'], [], [], []])
        event = VersionEvent.objects.first()
        self.assertEqual((event.action, event.version, event.other_version),
                         (EventAction.import_version, 'copy', 'src'))

    def test_import_as_tag(self):
        settings = import_version(io.BytesIO(export_bytes()), 't',
                                  editable=False, description='a tag')
        self.assertFalse(settings.editable)
        self.assertEqual(settings.description, 'a tag')
        self.assertEqual(SourceTerm.select_version('t').count(),
                         SourceTerm.select_version('src').count())

    def refused(self, data, name='new', exception=VersionFileError):
        before = snapshot()
        with self.assertRaises(exception) as cm:
            import_version(io.BytesIO(data), name)
        self.assertEqual(snapshot(), before)
        return cm.exception

    def test_bad_names(self):
        data = export_bytes()
        self.refused(data, 'src', KeyError)
        self.refused(data, 'bad/name', ValueError)

    def test_bad_files(self):
        good = read_members(export_bytes())
        manifest = json.loads(good['manifest.json'])

        def with_(**changes):
            members = dict(good)
            for name, value in changes.items():
                name = name.replace('__', '.')
                if value is None:
                    del members[name]
                else:
                    members[name] = value
            return make_file(members)
        cases = {
            'not a tar': b'hello',
            'no manifest': with_(manifest__json=None),
            'no settings': with_(settings__json=None),
            'unknown member': with_(**{'other__txt': b'x'}),
            'wrong format': with_(manifest__json=json.dumps(
                dict(manifest, format='other')).encode()),
            'newer format': with_(manifest__json=json.dumps(
                dict(manifest, format_version=99)).encode()),
            'count mismatch': with_(manifest__json=json.dumps(
                dict(manifest, counts=dict(manifest['counts'],
                                           Component=5))).encode()),
        }
        lines = good['component.jsonl'].splitlines()
        first = json_util.loads(lines[0])
        cases['bad json'] = with_(component__jsonl=b'{x\n' + b'\n'.join(
            lines[1:]))
        cases['duplicate'] = with_(component__jsonl=b'\n'.join(
            lines + [lines[0]]), manifest__json=json.dumps(manifest).encode())
        cases['wrong class'] = with_(component__jsonl=b'\n'.join(
            [json_util.dumps(dict(first, _cls='EmissionSpec')).encode()]
            + lines[1:]))
        cases['invalid'] = with_(component__jsonl=b'\n'.join(
            [json_util.dumps(dict(first, mass='not a mass')).encode()]
            + lines[1:]))
        cases['dangling'] = with_(component__jsonl=b'\n'.join(
            [json_util.dumps(dict(first, specs=[ObjectId()])).encode()]
            + lines[1:]))
        for case, data in cases.items():
            with self.subTest(case=case):
                self.refused(data)

    def test_problems_listed(self):
        good = read_members(export_bytes())
        lines = [json_util.loads(line)
                 for line in good['component.jsonl'].splitlines()]
        members = dict(good, **{'component.jsonl': b'\n'.join(
            json_util.dumps(dict(son, specs=[ObjectId()])).encode()
            for son in lines)})
        error = self.refused(make_file(members))
        self.assertEqual(len([p for p in error.problems
                              if "aren't in the file" in p]), 3)

    def test_rollback(self):
        data = export_bytes()
        before = snapshot()
        with mock.patch('bgexplorer.models.maintenance.rebuild_sourceterms',
                        side_effect=RuntimeError('boom')):
            with self.assertRaises(VersionFileError) as cm:
                import_version(io.BytesIO(data), 'new')
        self.assertIn('boom', str(cm.exception))
        self.assertEqual(snapshot(), before)

    def test_read_only_checks(self):
        manifest, settings, docs = read_version_file(
            io.BytesIO(export_bytes()))
        self.assertEqual(manifest['version_tag'], 'src')
        self.assertEqual(len(docs[Component]), 3)
