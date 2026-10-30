""" Filtered budget breakdowns for the results dashboard """
import unittest
from unittest import mock
from bgexplorer.models.budget import (BudgetFilter, dashboard, table,
                                      term_values)
from bgexplorer.models.component import Component, Assembly, Placement
from bgexplorer.models.emissionspec import (EmissionSpec, EmissionSource,
                                            SourceCategory)
from bgexplorer.models.termcalc import TermCalculator
from bgexplorer.models.sourceterm import (SourceTerm, clear_results_cache,
                                          NO_CATEGORY)
from bgexplorer.models.common import (DimensionalityError, UnitMismatchError,
                                      units as unitreg)
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.settings import get_settings, HitEffConfig
from bgexplorer.models.sourceterm import find_sourceterms
from bgexplorer.models import versioncontrol as vc
from tests import results
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
        return results.for_object(obj, relativeto).scalars['v1']

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
            {'copper', 'steel', 'c3'})
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
        # c3 has no material, so is listed by its name
        self.assertEqual(materials, {'copper': True, 'steel': False,
                                     'c3': False})

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
        expected = results.from_sourceterms(sts).scalars['v1']
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
        self.assertNotIn('c3', {row['key'] for row in
                                result['charts']['material']})
        with self.assertRaises(ValueError):
            dashboard(self.a2, 'v1', BudgetFilter(root=[self.p_c1]))

    def test_confidence(self):
        """ Rows have the size-weighted mean rating of their terms """
        for name, rating in (('c1', 5), ('c2', 2)):
            comp = Component.select_version('main').get(name=name)
            comp.confidence_rating = rating
            comp.save()
        result = dashboard(self.a2, 'v1')
        isotopes = {row['key']: row for row in result['charts']['isotope']}
        # c1 4 (rated 5), c2 3 (rated 2), c3 0.5 (unrated)
        self.assertAlmostEqual(isotopes['Th232']['confidence']['mean'],
                               (4 * 5 + 3 * 2) / 7)
        self.assertAlmostEqual(isotopes['Th232']['confidence']['unrated'],
                               0.5 / 7.5)
        # a limit is weighted by its upper limit
        limit = isotopes['K40']['size']
        self.assertGreater(limit, 20.)
        self.assertEqual(isotopes['K40']['confidence'],
                         dict(mean=5., unrated=0.))

        # an assembly averages all the terms below it, not its children
        rows = {row['label']: row for row in result['charts']['component']}
        self.assertAlmostEqual(rows['a1']['confidence']['mean'],
                               (5 * (4 + limit) + 2 * 3) / (7 + limit))
        self.assertEqual(rows['a1']['confidence']['unrated'], 0.)
        self.assertEqual(rows['c2 label']['confidence'],
                         dict(mean=2., unrated=0.))
        self.assertEqual(rows['c3']['confidence'],
                         dict(mean=None, unrated=1.))

        # only the terms passing the filters count
        f = BudgetFilter(dict(material=[('copper', False)]))
        rows = {row['label']: row for row in
                dashboard(self.a2, 'v1', f)['charts']['component']}
        self.assertEqual(rows['a1']['confidence'], dict(mean=5., unrated=0.))
        self.assertIsNone(rows['c2 label']['value'])
        self.assertEqual(rows['c2 label']['confidence'],
                         dict(mean=None, unrated=0.))
        # and the unfiltered size is still what orders them
        f = BudgetFilter(dict(isotope=[('K40', True)]))
        isotopes = {row['key']: row for row in
                    dashboard(self.a2, 'v1', f)['charts']['isotope']}
        self.assertAlmostEqual(isotopes['Th232']['confidence']['mean'],
                         (4 * 5 + 3 * 2) / 7)

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
        terms = term_values(self.a2)
        self.assertIs(term_values(self.a2), terms)
        # every scalar of each term at once
        self.assertEqual({tuple(t.values) for t in terms}, {('v1',)})
        # and each filtered result
        text = '{"material": [{"key": "copper"}]}'
        result = dashboard(self.a2, 'v1', BudgetFilter.from_json(text))
        self.assertIs(dashboard(self.a2, 'v1', BudgetFilter.from_json(text)),
                      result)
        self.assertIsNot(dashboard(self.a2, 'v1'), result)
        self.assertIs(table(self.a2), table(self.a2))
        self.c3.mass = '2 kg'
        self.c3.save()
        self.assertIsNot(term_values(self.a2), terms)
        self.assertIsNot(dashboard(self.a2, 'v1', BudgetFilter.from_json(text)),
                         result)

    def recalculated(self, **kwargs):
        """ The Terms of a2 and how many SourceTerms had to be calculated """
        with mock.patch.object(TermCalculator, 'scalars', autospec=True,
                               side_effect=TermCalculator.scalars
                               ) as calculate:
            return term_values(self.a2, **kwargs), calculate.call_count

    def test_unchanged_terms_reused(self):
        terms, count = self.recalculated()
        self.assertEqual(count, 5)
        self.assertEqual(self.recalculated()[1], 0)
        # editing one component only recalculates its own SourceTerms
        self.c3.confidence_rating = 2
        self.c3.save()
        edited, count = self.recalculated()
        self.assertEqual(count, 1)
        self.assertIsNot(edited, terms)
        # as does a change in a rate
        self.e2.reload()
        self.e2.sources[0].rate = '2 +- 0.5 mBq/kg'
        self.e2.save()
        self.assertEqual(self.recalculated()[1], 1)
        # the result is the same as calculating everything
        reused = dashboard(self.a2, 'v1')
        clear_results_cache()
        self.assertEqual(self.recalculated()[1], 5)
        fresh = dashboard(self.a2, 'v1')
        self.assertSame(reused['total'], fresh['total'])
        self.assertEqual(reused['charts']['component'][-1]['key'],
                         fresh['charts']['component'][-1]['key'])
        # HitEfficiencies could change any of them
        clear_results_cache()
        term_values(self.a2)
        self.replace_hiteff('c3', v1='0.7 +- 0.05 dru/mBq')
        self.assertEqual(self.recalculated()[1], 5)

    def test_unstamped_terms(self):
        """ SourceTerms from before there were stamps are stamped when used """
        SourceTerm._get_collection().update_many({}, {'$unset': {'stamp': ''}})
        expected = dashboard(self.a2, 'v1')
        clear_results_cache()
        self.assertEqual(self.recalculated()[1], 5)
        self.assertEqual(SourceTerm.objects(stamp__exists=True).count(), 5)
        self.assertEqual(self.recalculated()[1], 0)
        self.assertSame(dashboard(self.a2, 'v1')['total'], expected['total'])

    def replace_hiteff(self, location, **scalars):
        HitEfficiency.objects.get(source='Th232', location=location).delete()
        HitEfficiency(source='Th232', location=location,
                      scalars=scalars).save()

    def test_mixed_units(self):
        """ Results are summed in the display units whatever their units
        were """
        expected = dashboard(self.a2, 'v1')
        self.replace_hiteff('c3', v1='500 +- 50 dru/Bq')
        result = dashboard(self.a2, 'v1')
        self.assertIsNot(result, expected)
        self.assertEqual(result['units'], expected['units'])
        self.assertSame(result['total'], expected['total'])
        for new, old in zip(result['charts']['component'],
                            expected['charts']['component']):
            self.assertSame(new['value'], old['value'])
        self.assertSame(dashboard(self.a2, 'v1', unit='mdru')['total'],
                        expected['total'])
        with self.assertRaises(DimensionalityError):
            dashboard(self.a2, 'v1', unit='kg')

    def test_mismatched_units(self):
        """ Saved data that doesn't fit the display units is an error, not a
        wrong sum
        """
        self.replace_hiteff('c1', v1='0.1 +- 0.01 dru/mBq',
                            other='1 +- 0.1 dru/mBq')
        self.replace_hiteff('c3', v1='0.5 +- 0.05 dru/mBq',
                            other='1 +- 0.1 dru/mBq')
        # validation rejects this, so go around it
        hiteff = HitEfficiency.objects.get(source='Th232', location='c3')
        hiteff.scalars['other'] = unitreg.Quantity(
            hiteff.scalars['other'].m, '1/mBq')
        hiteff.save(validate=False)
        with self.assertRaises(UnitMismatchError):
            dashboard(self.a2, 'v1')
        with self.assertRaises(UnitMismatchError):
            dashboard(self.a2, 'other')

    def show_columns(self):
        settings = get_settings('main')
        settings.hiteffdbconfig.display_scalars = dict(
            v1=HitEffConfig(display_name='Value', display_unit='mdru'),
            hidden=HitEffConfig(hide=True))
        settings.save()

    def test_table(self):
        self.show_columns()
        result = table(self.a2)
        self.assertEqual([(c['key'], c['name'], str(c['units']))
                          for c in result['columns']],
                         [('v1', 'Value', 'mdru')])
        self.assertEqual((result['count'], result['ntotal']), (4, 4))
        # the root's total, then two levels of placements in order
        rows = result['rows']
        self.assertEqual(self.labels(rows), [
            ('a2', 0), ('a1', 1), ('c1', 2), ('c2 label', 2), ('c3', 1)])
        values = [row['values']['v1'] for row in rows]
        self.assertSame(values[0], self.total(self.a2))
        self.assertSame(values[1], self.total(self.a1, self.a2))
        self.assertAlmostEqual(values[2].to('mdru').m.nominal_value, 4000.)

        # only what passes every filter, including the component's
        result = table(self.a2, BudgetFilter(dict(
            material=[('copper', False)], component=[((self.p_a1,), False)])))
        self.assertEqual(result['count'], 2)
        values = [row['values']['v1'] for row in result['rows']]
        self.assertSame(values[0], self.total(self.c1, self.a2))
        self.assertSame(values[2], values[0])
        self.assertEqual([v is None for v in values],
                         [False, False, False, True, True])
        result = table(self.a2, BudgetFilter(dict(
            component=[((self.p_c3,), False)])))
        self.assertEqual([row['values']['v1'] is None
                          for row in result['rows']],
                         [False, True, True, True, False])

        # below a drilled down root
        result = table(self.a2, BudgetFilter(root=[self.p_a1]))
        self.assertEqual(self.labels(result['rows']), [
            ('a1', 0), ('c1', 1), ('c2 label', 1)])
        self.assertSame(result['rows'][0]['values']['v1'],
                        self.total(self.a1, self.a2))
        with self.assertRaises(ValueError):
            table(self.a2, BudgetFilter(root=['nope']))
        # a plain component is a single row
        self.assertEqual(self.labels(table(self.c1)['rows']), [('c1', 0)])

    def test_table_html(self):
        self.show_columns()
        page = self.html(self.client.get(self.url('component.results',
                                                  object=self.a2)))
        self.assertIn('<th>Value [mdru]</th>', page)
        self.assertIn('<tr class="component depth2">', page)
        self.assertNotIn('passing the budget filters', page)
        self.assertIn('resultstable', page)
        url = self.url('component.results_table', object=self.a2)
        page = self.html(self.client.get(url, query_string=dict(
            filters='{"material": [{"key": "copper"}]}')))
        self.assertIn('Only the 2 of 4', page)
        self.assertNotIn('<html', page)
        total = self.total(self.c1, self.a2).to('mdru').m
        self.assertIn('{:LS}'.format(total), page)
        response = self.client.get(url, query_string=dict(filters='nope'))
        self.assertEqual(response.status_code, 400)
        # relative to a parent assembly
        page = self.html(self.client.get(self.url(
            'component.results_table', object=self.a1, relativeto=self.a2)))
        self.assertIn('c2 label', page)
        self.assertNotIn('c3', page)

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
        # nothing is rated yet
        self.assertEqual(rows[1]['confidence'], dict(mean=None, unrated=1.))
        c1 = Component.select_version('main').get(name='c1')
        c1.confidence_rating = 4
        c1.save()
        data2 = self.client.get(url).get_json()
        self.assertEqual(data2['charts']['component'][1]['confidence'],
                         dict(mean=4., unrated=0.))
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
