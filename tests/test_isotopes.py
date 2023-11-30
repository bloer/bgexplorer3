import unittest
from bgexplorer.models.isotope import *

class TestIsotopes(unittest.TestCase):
    def test1_stringparse(self):
        self.assertIsNotNone(get_isotope('U238'))
        self.assertIsNotNone(get_isotope('238U'))
        self.assertIsNotNone(get_isotope('U-238'))
        self.assertIsNone(get_isotope('K4'))
        self.assertTrue(compare_source_names('U238', 'U-238'))
        self.assertTrue(compare_source_names('U238', '238U'))

    def test2_conctorate(self):
        self.assertAlmostEqual(concentration_to_rate('U238', 81*units.ppb),
                               1*units('Bq/kg'), 1)
        self.assertAlmostEqual(concentration_to_rate('Th232', 246*units.ppb),
                               1*units('Bq/kg'), 2)
        self.assertAlmostEqual(concentration_to_rate('K40', 32.3*units.ppm),
                               1*units('Bq/kg'), 2)
        self.assertAlmostEqual(concentration_to_rate('natK', 32.3*units.ppm),
                               1*units('Bq/kg'), 2)

    def test3_ratetoconc(self):
        self.assertAlmostEqual(rate_to_concentration('U238', 1*units('Bq/kg')),
                               81*units.ppb, 0)
        self.assertAlmostEqual(rate_to_concentration('Th232', 1*units('Bq/kg')),
                               246*units.ppb, 0)
        self.assertAlmostEqual(rate_to_concentration('K40', 1*units('Bq/kg')).to('ppm'),
                               32.3*units.ppm, 0)
