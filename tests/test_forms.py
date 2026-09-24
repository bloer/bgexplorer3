import unittest
from bson import ObjectId
from werkzeug.datastructures import MultiDict
from datetime import datetime
from bgexplorer.application.forms import update_object, LISTFIELDS_KEY
from bgexplorer.models.component import Component, HistoryEntry
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource


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
        c1 = update_object(Component(), form)
        self.assertEqual(len(c1.sources), 2)
        self.assertEqual(c1.sources[0].id, oldid)
        self.assertIsInstance(c1.sources[1].id, ObjectId)
        self.assertNotEqual(c1.sources[1].id, oldid)
        for source in c1.sources:
            source.clean()
            self.assertEqual(source.rate.m.id, source.id)

    def test2_missing_lists(self):
        """ Lists not in the form are left alone, but lists marked as
        present with no rows are cleared
        """
        c1 = Component(name='c1', sources=[EmissionSource(name='K40')],
                       history=[HistoryEntry(date=datetime(2024, 1, 1),
                                             description='made')])
        c1 = update_object(c1, MultiDict([('name', 'c2')]))
        self.assertEqual(c1.name, 'c2')
        self.assertEqual(len(c1.sources), 1)
        self.assertEqual(len(c1.history), 1)

        form = MultiDict([('name', 'c2'),
                          (LISTFIELDS_KEY, 'history'),
                          (LISTFIELDS_KEY, 'specs')])
        c1.specs = [EmissionSpec(name='e1')]
        c1 = update_object(c1, form)
        self.assertEqual(len(c1.sources), 1)
        self.assertEqual(len(c1.history), 0)
        self.assertEqual(len(c1.specs), 0)

if __name__ == '__main__':
    unittest.main()
