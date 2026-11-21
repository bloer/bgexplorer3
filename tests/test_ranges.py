import unittest
from bgexplorer.models.common import units
from bgexplorer.models.ranges import parse_ranges, merge_ranges


class TestRanges(unittest.TestCase):
    def kev(self, text, default_unit=None):
        return [(lo.to('keV').m, hi.to('keV').m)
                for lo, hi in parse_ranges(text, default_unit)]

    def test_parse(self):
        self.assertEqual(self.kev("10-15, 18-30 keV"), [(10, 15), (18, 30)])
        self.assertEqual(self.kev("10 keV - 15 keV; 18 to 30 keV"),
                         [(10, 15), (18, 30)])

    def test_units(self):
        # a range without a unit takes that of the next with one
        self.assertEqual(self.kev("1-2 MeV, 500-600 keV"),
                         [(1000, 2000), (500, 600)])
        self.assertEqual(self.kev("10-15, 1-2 MeV"), [(10000, 15000),
                                                      (1000, 2000)])
        self.assertEqual(self.kev("500 eV-2 keV"), [(0.5, 2)])
        self.assertEqual(self.kev("10-15", default_unit='keV'), [(10, 15)])
        self.assertEqual(self.kev("1e1-1.5e1 keV"), [(10, 15)])

    def test_empty(self):
        self.assertEqual(parse_ranges(""), [])
        self.assertEqual(parse_ranges(None), [])
        self.assertEqual(parse_ranges(" , ; "), [])

    def test_errors(self):
        for text in ("abc", "10", "10-", "10-20 frobnitz", "10-15 keV, x",
                     "10-20-30 keV"):
            with self.assertRaises(ValueError, msg=text):
                parse_ranges(text, 'keV')
        # no unit anywhere
        with self.assertRaises(ValueError):
            parse_ranges("10-15")

    def test_merge(self):
        self.assertEqual(merge_ranges([]), [])
        self.assertEqual(merge_ranges([(5, 6), (1, 3), (2, 4), (4, 4.5)]),
                         [(1, 4.5), (5, 6)])
        self.assertEqual(merge_ranges([(1, 10), (2, 3)]), [(1, 10)])
        self.assertEqual(merge_ranges([(3, 3), (5, 4)]), [])


if __name__ == '__main__':
    unittest.main()
