""" Comparing documents and versions """
import unittest
from mongoengine import disconnect
from bgexplorer.models import versioncontrol as vc
from bgexplorer.models.versioncontrol import _versioned_classes
from bgexplorer.models.versiondiff import (diff_values, diff_documents,
                                           compare_document, diff_versions)
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.settings import VersionSettings
from tests.dbutil import connect_test_db
from tests.test_app_components import AppTestCase


def entries(left, right):
    return [(e.pathstr, e.kind, e.left, e.right)
            for e in diff_values(left, right)]


class TestDiffValues(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(entries({'a': 1, 'b': 2}, {'a': 1, 'b': 2}), [])
        self.assertEqual(entries({'a': 1, 'b': 2}, {'a': 1, 'b': 3, 'c': 4}),
                         [('b', 'changed', 2, 3), ('c', 'added', None, 4)])
        self.assertEqual(entries({'a': 1}, {}), [('a', 'removed', 1, None)])

    def test_quantities_are_leaves(self):
        self.assertEqual(
            entries({'m': {'value': 1, 'units': 'kg'}},
                    {'m': {'value': 2, 'units': 'kg'}}),
            [('m', 'changed', {'value': 1, 'units': 'kg'},
              {'value': 2, 'units': 'kg'})])
        self.assertEqual(entries({'r': {'str': '1 Bq', 'id': 'x'}},
                                 {'r': {'str': '2 Bq', 'id': 'x'}})[0][:2],
                         ('r', 'changed'))

    def test_embedded_lists_by_id(self):
        left = {'sources': [{'id': 1, 'name': 'K40', 'rate': 1},
                            {'id': 2, 'name': 'U238', 'rate': 2},
                            {'id': 3, 'name': 'Th232', 'rate': 3}]}
        # reordered, one changed, one removed, one added
        right = {'sources': [{'id': 2, 'name': 'U238', 'rate': 2},
                             {'id': 1, 'name': 'K40', 'rate': 5},
                             {'id': 4, 'name': 'Co60', 'rate': 4}]}
        result = entries(left, right)
        self.assertEqual([r[:2] for r in result], [
            ('sources[K40].rate', 'changed'),
            ('sources[Th232]', 'removed'),
            ('sources[Co60]', 'added')])

    def test_plain_lists(self):
        # lists of plain values, e.g. references, are compared whole
        self.assertEqual(entries({'specs': [1, 2]}, {'specs': [2, 1, 3]}),
                         [('specs', 'changed', [1, 2], [2, 1, 3])])
        # lists of documents without ids by position
        self.assertEqual([r[:2] for r in entries(
            {'h': [{'a': 1}, {'a': 2}]}, {'h': [{'a': 1}]})],
            [('h[1]', 'removed')])


class TestCompareDocuments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        VersionSettings.drop_collection()
        for cls in _versioned_classes:
            cls.drop_collection()
        vc.create_version('main')
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='K40', rate='1 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='1 kg', specs=[self.e1]).save()
        vc.create_version('b', 'main')

    def test_shared_copy(self):
        result = compare_document(Component, self.c1.original_id,
                                  'main', 'b')
        self.assertTrue(result.same_copy)
        self.assertTrue(result.identical)
        self.assertEqual(result.entries, [])

    def test_changed_copy(self):
        c1 = Component.select_version('b').get(name='c1')
        c1.mass = '2 kg'
        c1.save()
        result = compare_document(Component, self.c1.original_id,
                                  'main', 'b')
        self.assertFalse(result.same_copy)
        self.assertEqual([(e.pathstr, e.kind) for e in result.entries],
                         [('mass', 'changed')])
        # the revision and modification time don't count as changes
        c1.mass = '1 kg'
        c1.save()
        result = compare_document(Component, self.c1.original_id,
                                  'main', 'b')
        self.assertFalse(result.same_copy)
        self.assertTrue(result.identical)

    def test_missing(self):
        c2 = Component(name='c2', version_tag='b').save()
        result = compare_document(Component, c2.original_id, 'main', 'b')
        self.assertIsNone(result.left)
        self.assertIsNotNone(result.right)
        self.assertFalse(result.identical)

    def test_diff_documents(self):
        e1b = EmissionSpec.select_version('b').get(name='e1')
        e1b.sources[0].rate = '2 mBq/kg'
        e1b.sources.append(EmissionSource(name='Co60', rate='1 mBq/kg'))
        e1b.save()
        e1 = EmissionSpec.select_version('main').get(name='e1')
        self.assertEqual([(e.pathstr, e.kind)
                          for e in diff_documents(e1, e1b)],
                         [('sources[K40].rate', 'changed'),
                          ('sources[Co60]', 'added')])


class TestDiffPages(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.e1 = EmissionSpec(name='e1', sources=[
            EmissionSource(name='K40', rate='1 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='1 kg', specs=[self.e1]).save()
        self.a1 = Assembly(name='a1', children=[
            Placement(component=self.c1, weight=2)]).save()
        vc.create_version('b', 'main')

    def diff_url(self, endpoint, obj, other, version='main'):
        return self.url(f'{endpoint}.diff', version,
                        itemid=str(obj.original_id), other_version=other)

    def test_diff_page(self):
        c1 = Component.select_version('b').get(name='c1')
        c1.mass = '2 kg'
        c1.specs = []
        c1.save()
        response = self.client.get(self.diff_url('component', c1, 'b'))
        self.assertEqual(response.status_code, 200)
        html = self.html(response)
        self.assertIn('id="difftable"', html)
        self.assertIn('class="diff-changed', html)
        self.assertIn('<td>1 kg</td>', html)
        self.assertIn('<td>2 kg</td>', html)
        # references are linked by name
        self.assertIn('>e1</a>', html)
        # the other versions table links to it
        html = self.html(self.client.get(
            self.url('component.view', 'main', objid=c1.original_id)))
        self.assertIn(self.diff_url('component', c1, 'b'), html)

    def test_same_and_missing(self):
        html = self.html(self.client.get(
            self.diff_url('component', self.a1, 'b')))
        self.assertIn('share the same copy', html)
        c2 = Component(name='c2', version_tag='b').save()
        response = self.client.get(self.diff_url('component', c2, 'b'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('only in b', self.html(response))

    def test_not_found(self):
        for url in (self.diff_url('component', self.c1, 'nosuch'),
                    self.url('component.diff', itemid='bad',
                             other_version='b'),
                    self.url('component.diff', itemid=str(self.e1.id),
                             other_version='b')):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)


def branch_with_changes():
    """ 'main', and 'b' with one of each kind of change to components """
    vc.create_version('main')
    e1 = EmissionSpec(name='e1').save()
    for name in ('same', 'equal', 'changed', 'deleted'):
        Component(name=name, mass='1 kg', specs=[e1]).save()
    vc.create_version('b', 'main')
    for name, mass in (('equal', '1 kg'), ('changed', '2 kg')):
        c = Component.select_version('b').get(name=name)
        c.mass = mass
        c.save()
    Component.select_version('b').get(name='deleted').delete()
    Component(name='added', version_tag='b').save()
    Component(name='mainonly', version_tag='main').save()
    settings = vc.get_settings('b')
    settings.hiteffdbconfig.extra_columns = ['material']
    settings.save()


class TestCompareVersions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        VersionSettings.drop_collection()
        for cls in _versioned_classes:
            cls.drop_collection()
        branch_with_changes()

    def test_categories(self):
        result = diff_versions('main', 'b')
        components = result.classes[0]
        self.assertIs(components.cls, Component)

        def names(items):
            return [i.name for i in items]
        self.assertEqual(names(components.only_left), ['deleted', 'mainonly'])
        self.assertEqual(names(components.only_right), ['added'])
        self.assertEqual(names(components.differ), ['changed'])
        self.assertEqual(names(components.equal), ['equal'])
        self.assertEqual(names(components.same), ['same'])
        self.assertEqual(components.differ[0].newer, 'right')
        self.assertFalse(result.classes[1].changed)
        self.assertEqual([e.pathstr for e in result.settings],
                         ['hiteffdbconfig.extra_columns'])
        self.assertTrue(result.changed)
        # without checking content, equal copies count as changed
        components = diff_versions('main', 'b', check_content=False)\
            .classes[0]
        self.assertEqual(names(components.differ), ['changed', 'equal'])

    def test_unchanged(self):
        vc.create_version('c', 'b')
        self.assertFalse(diff_versions('b', 'c').changed)


class TestComparePages(AppTestCase):
    def test_compare(self):
        branch_with_changes()
        url = self.url('compare_versions', 'main', **{'with': 'b'})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = self.html(response)
        self.assertIn('1 changed', html)
        self.assertIn('2 only in main', html)
        self.assertIn('1 only in b', html)
        self.assertIn('hiteffdbconfig.extra_columns', html)
        added = Component.select_version('b').get(name='added')
        # items only in the other version link to its diff page, where they
        # can be imported
        self.assertIn(self.url('component.diff', 'main',
                               itemid=str(added.original_id),
                               other_version='b'), html)
        deleted = Component.select_version('main').get(name='deleted')
        self.assertIn(self.url('component.diff', 'b',
                               itemid=str(deleted.original_id),
                               other_version='main'), html)
        # the overview links here
        self.assertIn(url, self.html(self.client.get(
            self.url('overview', 'main'))))
        self.assertEqual(self.client.get(self.url(
            'compare_versions', 'main')).status_code, 200)
        self.assertEqual(self.client.get(self.url(
            'compare_versions', 'main', **{'with': 'nosuch'})).status_code,
            404)
