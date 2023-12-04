import unittest
from bgexplorer.models.common import *

class TestCommon(unittest.TestCase):
    def test_units(self):
        self.assertIn('dru', units)
        self.assertIn('kky', units)
        self.assertIn('ppb', units)
        self.assertIn('ppt', units)
        self.assertIn('ppq', units)
        self.assertIn('ppm', units)

    def test_addnone(self):
        self.assertEqual(addnone(3, None), 3)
        self.assertEqual(addnone(None, 3), 3)
        self.assertEqual(addnone(3,4), 7)
        self.assertIsNone(addnone(None, None))

    def test_multnone(self):
        self.assertEqual(multnone(3, None), 3)
        self.assertEqual(multnone(None, 3), 3)
        self.assertEqual(multnone(3, 4), 12)
        self.assertIsNone(multnone(None, None))

    def test_validate_units(self):
        with self.assertRaises(ValidationError):
            validate_unit(None, 'kg', allow_none=False)
        with self.assertRaises(ValidationError):
            validate_unit(10*units.s, 'kg')
        with self.assertRaises(ValidationError):
            validate_unit(3, 'kg')
        validate_unit(3 * units.g, 'kg')
