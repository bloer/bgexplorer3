import unittest
from bson import ObjectId
from werkzeug.datastructures import MultiDict
from bgexplorer.application.forms import update_object
from bgexplorer.models.component import Component


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


if __name__ == '__main__':
    unittest.main()
