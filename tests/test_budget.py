""" Filtered budget breakdowns for the results dashboard """
import unittest
from bgexplorer.models.budget import (BudgetFilter, dashboard, NO_CATEGORY,
                                      term_values)
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import (EmissionSpec, EmissionSource,
                                            SourceCategory)
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.sourceterm import CalculatedResults, find_sourceterms
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase


def keys(component=(), isotope='Th232', material='copper',
         category='assay'):
    return dict(component=tuple(component), isotope=isotope,
                material=material, category=category)


class TestBudgetFilter(unittest.TestCase):
    def test_empty(self):
        f = BudgetFilter()
        self.assertTrue(f.matches(keys()))
        self.assertEqual(f.todict(), {})
        self.assertTrue(BudgetFilter.from_json('').matches(keys()))

    def test_include_exclude(self):
        f = BudgetFilter(dict(isotope=[('Th232', False), ('K40', False)],
                              material=[('steel', True)]))
        # includes are OR'd
        self.assertTrue(f.matches(keys(isotope='K40')))
        self.assertFalse(f.matches(keys(isotope='U238')))
        # excludes reject
        self.assertFalse(f.matches(keys(material='steel')))
        # skip ignores one groupby's own entries
        self.assertTrue(f.matches(keys(isotope='U238'), skip='isotope'))
        self.assertFalse(f.matches(keys(isotope='U238'), skip='material'))
        self.assertTrue(f.passes('isotope', 'Th232'))
        self.assertTrue(f.passes('category', 'anything'))

    def test_component_paths(self):
        f = BudgetFilter(dict(component=[(['a'], False), (['a', 'b'], True)]))
        # matches everything below a placement
        self.assertTrue(f.matches(keys(['a'])))
        self.assertTrue(f.matches(keys(['a', 'c', 'd'])))
        self.assertFalse(f.matches(keys(['a', 'b', 'd'])))
        self.assertFalse(f.matches(keys(['c', 'a'])))
        self.assertFalse(f.matches(keys()))
        # everything must be below the root
        f = BudgetFilter(root=['a'])
        self.assertTrue(f.matches(keys(['a', 'b'])))
        self.assertFalse(f.matches(keys(['b'])))
        self.assertFalse(f.matches(keys(['b']), skip='component'))

    def test_json(self):
        text = ('{"root": ["a"], "component": [{"key": ["a", "b"]}],'
                ' "isotope": [{"key": "K40", "exclude": true}]}')
        f = BudgetFilter.from_json(text)
        self.assertEqual(f.root, ('a',))
        self.assertEqual(f.entries['component'], [(('a', 'b'), False)])
        self.assertEqual(f.entries['isotope'], [('K40', True)])
        self.assertEqual(f.todict(), dict(
            root=['a'], component=[dict(key=['a', 'b'], exclude=False)],
            isotope=[dict(key='K40', exclude=True)]))
        self.assertEqual(BudgetFilter.from_json('{"isotope": []}').entries,
                         {})
        for text in ('nope', '[]', '{"nope": []}', '{"isotope": "K40"}',
                     '{"isotope": [{"key": ["K40"]}]}',
                     '{"component": [{"key": "a"}]}', '{"root": "a"}',
                     '{"isotope": [{"exclude": true}]}'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    BudgetFilter.from_json(text)


class TestDashboard(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        HitEfficiency(source='Th232', location='c1',
                      scalars=dict(v1='0.1 +- 0.01 dru/mBq')).save()
        HitEfficiency(source='K40', location='c1',
                      scalars=dict(v1='0.2 +- 0.02 dru/mBq')).save()
        HitEfficiency(source='Th232', location='c2',
                      scalars=dict(v1='0.3 +- 0.03 dru/mBq')).save()
        HitEfficiency(source='Th232', location='c3',
                      scalars=dict(v1='0.5 +- 0.05 dru/mBq')).save()
        # c1 and c2 share e1, so their Th232 results are correlated
        self.e1 = EmissionSpec(name='e1', category=SourceCategory.assay,
                               sources=[
            EmissionSource(name='Th232', rate='10 +- 1 mBq/kg'),
            EmissionSource(name='K40', rate='<25 mBq/kg')]).save()
        self.e2 = EmissionSpec(name='e2', sources=[
            EmissionSource(name='Th232', rate='1 +- 0.5 mBq/kg')]).save()
        self.c1 = Component(name='c1', mass='2 kg', location='c1',
                            material='copper', specs=[self.e1]).save()
        self.c2 = Component(name='c2', mass='1 kg', location='c2',
                            material='steel', specs=[self.e1]).save()
        self.c3 = Component(name='c3', mass='1 kg', location='c3',
                            specs=[self.e2]).save()
        self.a1 = Assembly(name='a1', children=[
            Placement(component=self.c1, weight=2),
            Placement(component=self.c2, label='c2 label')]).save()
        self.a2 = Assembly(name='a2', children=[
            Placement(component=self.a1), Placement(component=self.c3)]).save()
        self.p_a1, self.p_c3 = (str(p.id) for p in self.a2.children)
        self.p_c1, self.p_c2 = (str(p.id) for p in self.a1.children)

    def total(self, obj, relativeto=None):
        return CalculatedResults.for_object(obj, relativeto, save=False,
                                            cache=False).scalars['v1']

    def assertSame(self, value, expected):
        value = value.to(expected.u)
        self.assertAlmostEqual(value.m.nominal_value, expected.m.nominal_value)
        self.assertAlmostEqual(value.m.get_upper_limit(0.9),
                               expected.m.get_upper_limit(0.9))

    def labels(self, rows):
        return [(row['label'], row['depth']) for row in rows]

    def test_unfiltered(self):
        result = dashboard(self.a2, 'v1')
        self.assertEqual(result['count'], result['ntotal'])
        self.assertEqual(result['count'], 4)
        self.assertSame(result['total'], self.total(self.a2))
        self.assertSame(result['unfiltered'], result['total'])
        self.assertEqual(result['breadcrumb'], [((), 'a2')])
        # two levels of components, children after their parent
        rows = result['charts']['component']
        self.assertEqual(self.labels(rows), [
            ('a1', 0), ('c1', 1), ('c2 label', 1), ('c3', 0)])
        self.assertEqual(rows[0]['key'], (self.p_a1,))
        self.assertEqual(rows[1]['key'], (self.p_a1, self.p_c1))
        self.assertTrue(all(row['selected'] for row in rows))
        self.assertEqual([row['children'] for row in rows],
                         [True, False, False, False])
        # c1: 2 x 2 kg x 10 mBq/kg x 0.1 dru/mBq, plus a limit from K40
        # (2 x 2 kg x 25 mBq/kg x 0.2 dru/mBq), summed together
        c1 = rows[1]['value']
        self.assertAlmostEqual(c1.m.nominal_value, 4.)
        self.assertFalse(c1.m.isupperlimit())
        self.assertGreater(c1.m.get_upper_limit(0.9), 20.)
        self.assertSame(rows[0]['value'], self.total(self.a1, self.a2))
        # a row of only limits is a limit
        isotopes = {row['key']: row for row in result['charts']['isotope']}
        self.assertTrue(isotopes['K40']['value'].m.isupperlimit())
        self.assertAlmostEqual(isotopes['K40']['size'],
                               isotopes['K40']['value'].m.get_upper_limit(0.9))
        self.assertAlmostEqual(isotopes['Th232']['size'], 4. + 3. + 0.5)
        # each breakdown adds up to the total
        for groupby in ('isotope', 'material', 'category'):
            with self.subTest(groupby=groupby):
                values = [row['value'] for row in result['charts'][groupby]]
                self.assertSame(sum(values[1:], values[0]), result['total'])
        self.assertEqual(
            {row['key'] for row in result['charts']['material']},
            {'copper', 'steel', '(no material)'})
        categories = {row['key']: row for row in result['charts']['category']}
        self.assertEqual(set(categories), {'assay', NO_CATEGORY})
        self.assertAlmostEqual(
            categories[NO_CATEGORY]['value'].m.nominal_value, 0.5)

    def test_filtered(self):
        f = BudgetFilter(dict(material=[('copper', False)]))
        result = dashboard(self.a2, 'v1', f)
        self.assertEqual(result['count'], 2)
        self.assertSame(result['total'], self.total(self.c1, self.a2))
        # other breakdowns only include copper
        isotopes = {row['key']: row for row in result['charts']['isotope']}
        self.assertAlmostEqual(isotopes['Th232']['value'].m.nominal_value, 4.)
        # rows stay put, but only copper's have values
        rows = result['charts']['component']
        self.assertEqual(self.labels(rows), [
            ('a1', 0), ('c1', 1), ('c2 label', 1), ('c3', 0)])
        self.assertEqual([row['value'] is not None for row in rows],
                         [True, True, False, False])
        self.assertTrue(all(row['selected'] for row in rows))
        # ordered by, and with, the unfiltered sizes
        unfiltered = dashboard(self.a2, 'v1')['charts']['component']
        self.assertEqual([row['size'] for row in rows],
                         [row['size'] for row in unfiltered])
        self.assertAlmostEqual(rows[1]['size'], 4.)
        self.assertSame(result['unfiltered'], self.total(self.a2))
        # but not the material breakdown itself, where the rest are dimmed
        materials = {row['key']: row['selected']
                     for row in result['charts']['material']}
        self.assertEqual(materials, {'copper': True, 'steel': False,
                                     '(no material)': False})

        # exclude a component, and everything below it
        f = BudgetFilter(dict(component=[((self.p_a1,), True)]))
        result = dashboard(self.a2, 'v1', f)
        self.assertSame(result['total'], self.total(self.c3, self.a2))
        self.assertEqual([row['selected'] for row in
                          result['charts']['component']],
                         [False, False, False, True])

    def test_correlated(self):
        # both Th232 terms come from the same assay
        f = BudgetFilter(dict(isotope=[('Th232', False)],
                              component=[((self.p_a1,), False)]))
        result = dashboard(self.a2, 'v1', f)
        sts = [st for st in find_sourceterms(self.a2)
               if st.source.name == 'Th232' and st.component.name != 'c3']
        expected = CalculatedResults.from_sourceterms(
            sts, allowcache=False, save=False).scalars['v1']
        self.assertSame(result['total'], expected)
        # which is more uncertain than adding them independently
        rows = {row['label']: row['value']
                for row in result['charts']['component']}
        independent = (rows['c1'].m.sigma**2 + rows['c2 label'].m.sigma**2)
        self.assertGreater(result['total'].m.sigma**2,
                           independent * 1.1)

    def test_root(self):
        f = BudgetFilter(root=[self.p_a1])
        result = dashboard(self.a2, 'v1', f)
        self.assertEqual(result['breadcrumb'],
                         [((), 'a2'), ((self.p_a1,), 'a1')])
        rows = result['charts']['component']
        self.assertEqual(self.labels(rows), [('c1', 0), ('c2 label', 0)])
        self.assertEqual(rows[0]['key'], (self.p_a1, self.p_c1))
        self.assertSame(result['total'], self.total(self.a1, self.a2))
        # the root applies to every breakdown
        self.assertNotIn('(no material)', {row['key'] for row in
                                           result['charts']['material']})
        with self.assertRaises(ValueError):
            dashboard(self.a2, 'v1', BudgetFilter(root=[self.p_c1]))

    def test_errors(self):
        with self.assertRaises(ValueError):
            dashboard(self.a2, 'nope')
        with self.assertRaises(ValueError):
            dashboard(self.a2, 'v1', groupbys=['nope'])
        result = dashboard(self.a2, 'v1', unit='mdru')
        self.assertAlmostEqual(result['total'].to('mdru').m.nominal_value,
                               result['total'].m.nominal_value)
        self.assertAlmostEqual(
            result['charts']['component'][1]['value'].m.nominal_value, 4000.)

    def test_own_sources(self):
        # a plain component has a single row, which can't be filtered on
        rows = dashboard(self.c1, 'v1')['charts']['component']
        self.assertEqual(self.labels(rows), [('c1', 0)])
        self.assertIsNone(rows[0]['key'])
        # a leaf child has no rows below it
        rows = dashboard(self.a1, 'v1')['charts']['component']
        self.assertEqual(self.labels(rows), [('c1', 0), ('c2 label', 0)])

    def test_cache(self):
        terms = term_values(self.a2, 'v1')
        self.assertIs(term_values(self.a2, 'v1'), terms)
        self.c3.mass = '2 kg'
        self.c3.save()
        self.assertIsNot(term_values(self.a2, 'v1'), terms)

    def test_json(self):
        url = self.url('component.dashboard_json', object=self.a2)
        data = self.client.get(url).get_json()
        self.assertEqual(data['scalar'], 'v1')
        self.assertEqual(data['units'], 'dru')
        self.assertEqual((data['count'], data['ntotal']), (4, 4))
        self.assertEqual(data['filters'], {})
        self.assertEqual(data['breadcrumb'], [dict(key=[], label='a2')])
        self.assertEqual(set(data['charts']),
                         {'component', 'isotope', 'material', 'category'})
        rows = data['charts']['component']
        self.assertEqual(rows[1]['key'], [self.p_a1, self.p_c1])
        self.assertEqual(rows[1]['depth'], 1)
        self.assertAlmostEqual(rows[1]['size'], 4.)
        self.assertEqual([row['children'] for row in rows],
                         [True, False, False, False])
        self.assertAlmostEqual(rows[1]['value']['value'], 4.)
        self.assertFalse(data['total']['is_limit'])
        self.assertGreater(data['total']['upper_limit'],
                           data['total']['value'])
        isotopes = {row['key']: row for row in data['charts']['isotope']}
        self.assertTrue(isotopes['K40']['value']['is_limit'])
        # totals are also formatted, like the contributions table
        total = self.total(self.a2).to('dru').m
        self.assertEqual(data['total']['text'], '{:S}'.format(total))
        self.assertEqual(data['total']['latex'], '{:LS}'.format(total))
        self.assertEqual(data['unfiltered']['text'], data['total']['text'])
        data = self.client.get(url, query_string=dict(
            filters='{"isotope": [{"key": "K40"}]}')).get_json()
        self.assertTrue(data['total']['is_limit'])
        self.assertTrue(data['total']['text'].startswith('<'))

        filters = ('{"root": ["%s"], "material": [{"key": "copper"}]}'
                   % self.p_a1)
        data = self.client.get(url, query_string=dict(
            filters=filters, unit='mdru')).get_json()
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['units'], 'mdru')
        self.assertGreater(data['unfiltered']['upper_limit'],
                           data['total']['upper_limit'])
        self.assertEqual(data['filters']['material'],
                         [dict(key='copper', exclude=False)])
        self.assertEqual([c['label'] for c in data['breadcrumb']],
                         ['a2', 'a1'])
        self.assertEqual([row['selected'] for row in
                          data['charts']['material']], [True, False])

        for query in (dict(scalar='nope'), dict(unit='kg'),
                      dict(filters='nope'), dict(filters='{"root": ["x"]}')):
            with self.subTest(query=query):
                response = self.client.get(url, query_string=query)
                self.assertEqual(response.status_code, 400)
                self.assertIn('message', response.get_json()['error'])
        # relative to a parent assembly
        data = self.client.get(self.url(
            'component.dashboard_json', object=self.a1,
            relativeto=self.a2)).get_json()
        self.assertEqual([row['label'] for row in data['charts']['component']],
                         ['c1', 'c2 label'])
