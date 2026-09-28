""" Importing documents from JSON files, and cloning documents """
import io
import json
import tarfile
import unittest
import zipfile
from bson import json_util
from mongoengine import disconnect
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.settings import VersionSettings
from bgexplorer.models.sourceterm import SourceTerm
from bgexplorer.models.importexport import iter_json_documents, \
    import_documents
from bgexplorer.models import versioncontrol as vc
from tests.dbutil import connect_test_db


def export(*docs) -> list:
    return [json_util.loads(doc.to_json()) for doc in docs]


class TestIterJson(unittest.TestCase):
    docs = [{'name': 'a'}, {'name': 'b'}]

    def test_single_and_list(self):
        self.assertEqual(list(iter_json_documents(io.BytesIO(b'{"a": 1}'))),
                         [{'a': 1}])
        data = json.dumps(self.docs).encode()
        self.assertEqual(list(iter_json_documents(io.BytesIO(data))),
                         self.docs)

    def test_archives(self):
        tarbuf = io.BytesIO()
        with tarfile.open(fileobj=tarbuf, mode='w:gz') as tar:
            for index, doc in enumerate(self.docs):
                data = json.dumps(doc).encode()
                info = tarfile.TarInfo(f'dir/{index}.json')
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        zipbuf = io.BytesIO()
        with zipfile.ZipFile(zipbuf, 'w') as archive:
            archive.writestr('one.json', json.dumps(self.docs[0]))
            archive.writestr('two.json', json.dumps([self.docs[1]]))
            archive.writestr('__MACOSX/._one.json', 'junk')
        for buf in (tarbuf, zipbuf):
            with self.subTest(buf=buf):
                buf.seek(0)
                self.assertEqual(list(iter_json_documents(buf)), self.docs)

    def test_bad_json(self):
        with self.assertRaises(ValueError):
            list(iter_json_documents(io.BytesIO(b'not json'), 'x.json'))
        with self.assertRaises(ValueError):
            list(iter_json_documents(io.BytesIO(b'[1, 2]')))


class TestImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in (Component, EmissionSpec, HitEfficiency, SourceTerm,
                    VersionSettings):
            cls.drop_collection()
        vc.create_version('main')
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='K40', rate='1 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='1 kg', specs=[self.e1]).save()
        self.c2 = Component(name='c2', mass='2 kg').save()
        self.a1 = Assembly(name='a1', children=[
            Placement(component=self.c1, weight=2),
            Placement(component=self.c2)]).save()
        vc.create_version('b')

    def test_assembly_with_children(self):
        """ an assembly imported with its children refers to the copies """
        report = import_documents(Component, export(self.a1, self.c1,
                                                    self.c2), 'b')
        self.assertEqual(report.errors, [])
        # 'b' is empty, so c1's spec is dropped
        self.assertEqual([label for label, _ in report.dropped_refs], ['c1'])
        self.assertEqual(len(report.created), 3)
        a1 = Assembly.select_version('b').get()
        self.assertNotEqual(a1.original_id, self.a1.original_id)
        ids = {p.component.original_id for p in a1.children}
        self.assertEqual(ids, {c.original_id for c in report.created
                               if c.name != 'a1'})
        self.assertEqual(a1.mass, self.a1.mass)
        self.assertIn('specs', report.dropped_refs[0][1])
        self.assertEqual(Component.select_version('b').get(name='c1').specs,
                         [])
        # the spec is kept in 'c', which has it
        vc.create_version('c', 'main')
        report = import_documents(Component, export(self.c1), 'c')
        self.assertEqual(report.dropped_refs, [])
        self.assertEqual(report.created[0].specs[0].name, 'e1')

    def test_existing_refs(self):
        """ references to documents already in the version are kept """
        report = import_documents(Component, export(self.a1), 'main')
        self.assertEqual(report.dropped_refs, [])
        (a1,) = report.created
        self.assertEqual([p.component.name for p in a1.children],
                         ['c1', 'c2'])
        self.assertEqual(Assembly.select_version('main').count(), 2)

    def test_unknown_refs(self):
        """ placements of unknown components are dropped """
        vc.create_version('empty')
        report = import_documents(Component, export(self.a1, self.c2),
                                  'empty')
        self.assertEqual(report.errors, [])
        self.assertEqual(len(report.dropped_refs), 1)
        self.assertIn('children', report.dropped_refs[0][1])
        a1 = Assembly.select_version('empty').get()
        self.assertEqual([p.component.name for p in a1.children], ['c2'])

    def test_errors(self):
        docs = export(self.c1, self.c2)
        docs[0]['mass'] = 'heavy'
        docs.append({'_cls': 'HitEfficiency', 'source': 'x'})
        report = import_documents(Component, docs, 'b')
        self.assertEqual([c.name for c in report.created], ['c2'])
        self.assertEqual(sorted(label for label, _ in report.errors),
                         ['#3', 'c1'])

    def test_clone(self):
        vc.create_version('c', 'main')
        a1 = Assembly.select_version('c').get()
        copy = a1.clone(name='a1 copy')
        self.assertIsNone(copy.original_id)
        copy.save()
        self.assertEqual(copy.version_tags, ['c'])
        self.assertNotEqual(copy.original_id, a1.original_id)
        self.assertEqual([p.component.original_id for p in copy.children],
                         [p.component.original_id for p in a1.children])
        self.assertTrue({p.id for p in copy.children}.isdisjoint(
            {p.id for p in a1.children}))
        self.assertEqual(Assembly.select_version('c').count(), 2)
        self.assertEqual(Assembly.select_version('main').count(), 1)
        c1 = Component.select_version('c').get(name='c1')
        copy = c1.clone()
        copy.save()
        self.assertEqual(copy.specs, c1.specs)
        with self.assertRaises(ValueError):
            Component.select_version('c').exclude('specs').first().clone()
