import unittest
from bson import ObjectId
from werkzeug.datastructures import MultiDict
from datetime import datetime
from bgexplorer.application.forms import update_object, LISTFIELDS_KEY
from bgexplorer.models.component import (Component, HistoryEntry,
                                         LocationOverride)
from bgexplorer.models.emissionspec import EmissionSpec


class TestForms(unittest.TestCase):
    def test1_embedded_ids(self):
        """ Existing embedded ids survive a form round trip, new rows get a
        fresh id, and rate correlation ids are linked to the source id
        """
        oldid = ObjectId()
        form = MultiDict([('name', 'c1'),
                          ('sources.id', str(oldid)),
                          ('sources.id', ''),
                          ('sources.name', 'K40'),
                          ('sources.name', 'U238'),
                          ('sources.rate', '1 mBq/kg'),
                          ('sources.rate', '2 mBq/kg'),
                          ])
        e1 = update_object(EmissionSpec(), form)
        self.assertEqual(len(e1.sources), 2)
        self.assertEqual(e1.sources[0].id, oldid)
        self.assertIsInstance(e1.sources[1].id, ObjectId)
        self.assertNotEqual(e1.sources[1].id, oldid)
        for source in e1.sources:
            source.clean()
            self.assertIsNotNone(source.rate.m.id)

    def test2_missing_lists(self):
        """ Lists not in the form are left alone, but lists marked as
        present with no rows are cleared
        """
        c1 = Component(name='c1', location_overrides=[
                           LocationOverride(source='K40', location='x')],
                       history=[HistoryEntry(date=datetime(2024, 1, 1),
                                             description='made')])
        c1 = update_object(c1, MultiDict([('name', 'c2')]))
        self.assertEqual(c1.name, 'c2')
        self.assertEqual(len(c1.location_overrides), 1)
        self.assertEqual(len(c1.history), 1)

        form = MultiDict([('name', 'c2'),
                          (LISTFIELDS_KEY, 'history'),
                          (LISTFIELDS_KEY, 'specs')])
        c1.specs = [EmissionSpec(name='e1')]
        c1 = update_object(c1, form)
        self.assertEqual(len(c1.location_overrides), 1)
        self.assertEqual(len(c1.history), 0)
        self.assertEqual(len(c1.specs), 0)

if __name__ == '__main__':
    unittest.main()


class TestKeyedMaps(unittest.TestCase):
    """ MapFields submitted as parallel __key__ / __value__ lists """
    def form(self, *rows, **extra):
        items = [(LISTFIELDS_KEY, 'scalars')]
        for key, value in rows:
            items += [('scalars.__key__', key), ('scalars.__value__', value)]
        return MultiDict(items + list(extra.items()))

    def test_add_rename_remove(self):
        from bgexplorer.models.hiteff import HitEfficiency
        hiteff = HitEfficiency(source='K40', location='c1',
                               scalars=dict(a='1 dru/mBq', b='2 dru/mBq'))
        update_object(hiteff, self.form(('a2', '1 dru/mBq'),
                                        ('c', '3 +- 1 dru/mBq')))
        self.assertEqual(list(hiteff.scalars), ['a2', 'c'])
        self.assertAlmostEqual(hiteff.scalars['c'].m.nominal_value, 3)
        self.assertTrue(hiteff.scalars['c'].u.is_compatible_with('dru/mBq'))
        # an empty table clears the map
        update_object(hiteff, self.form())
        self.assertEqual(hiteff.scalars, {})
        # not in the form: unchanged
        hiteff.scalars = dict(a='1 dru/mBq')
        update_object(hiteff, MultiDict([('source', 'U238')]))
        self.assertEqual(list(hiteff.scalars), ['a'])

    def test_errors(self):
        from bgexplorer.models.hiteff import HitEfficiency
        hiteff = HitEfficiency(source='K40', location='c1',
                               scalars=dict(a='1 dru/mBq'))
        errors = {}
        update_object(hiteff, self.form(('a', '1 dru/mBq'), ('a', '2 dru/mBq'),
                                        ('', '3 dru/mBq'), ('b', 'nonsense'),
                                        ('', '')),
                      errors=errors)
        self.assertEqual(sorted(errors), ['scalars.1', 'scalars.2',
                                          'scalars.b'])
        self.assertIn('Duplicate', errors['scalars.1'])
        self.assertEqual(list(hiteff.scalars), ['a'])
        with self.assertRaises(ValueError):
            update_object(hiteff, self.form(('', '1 dru/mBq')))
