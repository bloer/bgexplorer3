""" Reading radiopurity.org search results """
import os
import unittest
from pathlib import Path
from unittest import mock
import mongoengine
import requests
from bgexplorer.models import radiopurity as rp
from bgexplorer.models.assay import Assay
from bgexplorer.models.fields import UncertainQuantityField, get_fromstr
from bgexplorer.models import versioncontrol as vc
from bgexplorer.models.settings import VersionSettings
from tests.dbutil import TEST_MONGODB_URI, check_test_db

DATA = Path(__file__).parent / 'data'


def fixture(name):
    return (DATA / f'radiopurity_{name}.html').read_text(encoding='utf-8')


def parse(name):
    return rp.parse_results(fixture(name))


def byid(records):
    return {r.id: r for r in records}


class TestNormalizeIsotope(unittest.TestCase):
    def test_names(self):
        for name, expected in [
                ('U-238', 'U-238'), ('238U', 'U-238'), ('U238', 'U-238'),
                ('CO-60', 'Co-60'), ('40K', 'K-40'), ('60 Co', 'Co-60'),
                # shorthand for the head of the chain
                ('U', 'U-238'), ('Th', 'Th-232'), ('K', 'K-40'),
                ('Rb', None), ('C-60', None), ('Ra-228(Th-232)', None),
                ('Top of 238 U Chain', None), ('60 Co Chain', None),
                ('', None)]:
            with self.subTest(name=name):
                self.assertEqual(rp.normalize_isotope(name), expected)


class TestParseResults(unittest.TestCase):
    def test_mumetal(self):
        records, warnings = parse('mumetal')
        self.assertEqual(warnings, [])
        self.assertEqual(len(records), 3)
        record = byid(records)['60b4f3ecae890f84b01b2d7f']
        self.assertEqual(record.name, 'Mumetal')
        raw = record.raw
        self.assertEqual(raw['grouping'], 'ILIAS ANAIS')
        self.assertEqual(raw['sample']['id'], 'Conc. #18')
        self.assertEqual(raw['measurement']['technique'], 'Ge')
        self.assertEqual(raw['measurement']['institution'], 'LSC')
        self.assertEqual(raw['measurement']['practitioner']['name'],
                         'J.Puimedón & A.Ortiz')
        self.assertEqual(raw['data_source']['reference'],
                         'ILIAS Database http://radiopurity.in2p3.fr/')
        self.assertEqual(raw['measurement']['results'][0],
                         dict(isotope='Th-234', type='limit',
                              value=['1000'], unit='mBq/kg'))
        self.assertEqual(str(record.values[0]), 'Th-234: < 1000 mBq/kg')

    def test_row_formats(self):
        records, _ = parse('copper')
        records = byid(records)
        values = {v.isotope: v for v in
                  records['60e5e930ff9b7ec89ff87f31'].values}
        # symmetric error
        self.assertEqual(values['K-40'].rate, '20 +- 10 mBq/kg')
        self.assertEqual(values['U-238'].rate, '< 23.9 mBq/kg')
        self.assertEqual(values['CO-60'].name, 'Co-60')
        values = {v.isotope: v for v in
                  records['60b4f3ecae890f84b01b2cb3'].values}
        # limit with a confidence level, shorthand names
        self.assertEqual(values['Th'].rate, '< 5 (95%) ppt')
        self.assertEqual(values['Th'].name, 'Th-232')
        self.assertEqual(values['K'].rate, '0.4 ppb')
        self.assertEqual(values['K'].name, 'K-40')

    def test_template_formats(self):
        """ Formats the template can render, but that aren't in the live
        fixtures
        """
        records, _ = parse('shapes')
        values = {v.isotope: v for v in records[0].values}
        self.assertEqual(values['U-238'].rate, '(12 +3 -2) mBq/kg')
        # its <td> is left open in the template
        self.assertEqual(values['Co-60'].rate, '4 mBq/kg')
        self.assertEqual(values['Th-232'].rate, '< 2 (68%) ppb')
        for isotope, value in [('K-40', ['1']), ('Cs-137', ['1', '5']),
                               ('Pb-210', ['1', '5', '90'])]:
            with self.subTest(isotope=isotope):
                self.assertIsNone(values[isotope].rate)
                self.assertIn("ranges", values[isotope].warning)
                raw = next(r for r in records[0].raw['measurement']['results']
                           if r['isotope'] == isotope)
                self.assertEqual(raw['type'], 'range')
                self.assertEqual(raw['value'], value)

    def test_warnings(self):
        records, _ = parse('copper')
        records = byid(records)
        pins = records['60e5f6bc6fd19adee2f9d51d']
        self.assertTrue(all(v.rate is None for v in pins.values))
        self.assertIn("can't import units 'μBq/pair'", pins.values[0].warning)
        # pint reads this as milli-Bq per micro-nit
        weir = records['60b4f3ecae890f84b01b2cef']
        self.assertIn("'mBq/unit'", weir.values[0].warning)
        self.assertIsNone(weir.values[0].rate)
        # a named chain in Bq/kg is still a usable source
        legend = {v.isotope: v for v in
                  records['694301893d6d8ac2ea24d860'].values}
        chain = legend['Top of 238 U Chain']
        self.assertEqual(chain.name, 'Top of 238 U Chain')
        self.assertEqual(chain.rate, '8.16 +- 27.66 mBq/kg')
        self.assertTrue(chain.source)
        self.assertIn("not a recognized isotope", chain.warning)
        # but a concentration has to be of an isotope
        rb = next(v for v in records['60b4f3ecae890f84b01b2cbf'].values
                  if v.isotope == 'Rb')
        self.assertEqual(rb.rate, '2.6 ppb')
        self.assertFalse(rb.source)
        self.assertIn("not added as a source", rb.warning)

    def test_no_isotope(self):
        records, _ = parse('copper')
        values = [v for r in records for v in r.values if not v.isotope]
        self.assertEqual(len(values), 1)
        self.assertIsNone(values[0].rate)
        self.assertEqual(values[0].warning, "no isotope given")

    def test_rates_are_quantities(self):
        field = UncertainQuantityField()
        records, _ = parse('copper')
        for value in (v for r in records for v in r.values if v.rate):
            with self.subTest(rate=value.rate):
                quantity = field.to_python(value.rate)
                self.assertEqual(quantity.m.isupperlimit(),
                                 value.rate.startswith('<'))

    def test_count_mismatch(self):
        records, warnings = parse('shapes')
        self.assertEqual(len(records), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn("reports 2 records", warnings[0])

    def test_empty(self):
        self.assertEqual(rp.parse_results(
            '<div id="query-results-container"></div>'), ([], []))

    def test_errors(self):
        with self.assertRaisesRegex(rp.RadiopurityError, 'unexpected page'):
            rp.parse_results('<html><body>Maintenance</body></html>')
        page = ('<div class="section-container"><p class="normal-text">'
                'query error: bad query</p></div>'
                '<div id="query-results-container"></div>')
        with self.assertRaisesRegex(rp.RadiopurityError, 'bad query'):
            rp.parse_results(page)


class TestSearch(unittest.TestCase):
    @mock.patch('bgexplorer.models.radiopurity.requests.post')
    def test_request(self, post):
        post.return_value.text = fixture('mumetal')
        records, _ = rp.search('mumetal', include_synonyms=False)
        self.assertEqual(len(records), 3)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0],
                         'https://www.radiopurity.org/simple_search')
        self.assertEqual(post.call_args.kwargs['data'],
                         dict(query_field='all',
                              comparison_operator='contains',
                              query_value='mumetal'))
        rp.search('mumetal')
        self.assertEqual(post.call_args.kwargs['data']['include_synonyms'],
                         'true')

    @mock.patch('bgexplorer.models.radiopurity.requests.post')
    def test_connection_error(self, post):
        post.side_effect = requests.ConnectionError('no route to host')
        with self.assertRaisesRegex(rp.RadiopurityError, 'no route to host'):
            rp.search('mumetal')

    @unittest.skipUnless(os.environ.get('BGEXPLORER_TEST_RADIOPURITY'),
                         "set BGEXPLORER_TEST_RADIOPURITY to search the"
                         " live radiopurity.org")
    def test_live(self):
        """ Check that the live page can still be read """
        records, warnings = rp.search('mumetal')
        self.assertEqual(warnings, [])
        self.assertGreater(len(records), 0)
        self.assertTrue(all(r.id and r.values for r in records))


class TestToAssay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_test_db()
        mongoengine.connect(host=TEST_MONGODB_URI)

    @classmethod
    def tearDownClass(cls):
        mongoengine.disconnect()

    def setUp(self):
        for cls in (VersionSettings, Assay):
            cls.drop_collection()
        vc.create_version('main')

    def test_fields(self):
        records, _ = parse('copper')
        record = byid(records)['60e5e930ff9b7ec89ff87f31']
        assay = record.to_assay('main')
        assay.save()
        assay = Assay.objects.get()
        self.assertEqual(assay.name, 'Conductors (PTFE, Cu)')
        self.assertEqual(assay.radiopurityid, record.id)
        self.assertEqual(assay.category.value, 'assay')
        self.assertEqual(assay.description,
                         'Conductors (PTFE, Cu), 61.1g, Miscellaneous')
        self.assertEqual(assay.sample.vendor, 'Various')
        self.assertEqual(assay.measurement.institution, 'GeMPI IV')
        # fields radiopurity.org leaves out are empty
        self.assertFalse(assay.measurement.technique)
        self.assertEqual(assay.publication.org, 'XENON1T')
        sources = {s.name: s for s in assay.sources
                   if s.generated_from is None}
        self.assertEqual(set(sources), {
            'U-235', 'U-238', 'Ra-226', 'Ra-228(Th-232)', 'Th-228', 'K-40',
            'Co-60', 'Cs-137'})
        self.assertTrue(sources['U-238'].rate.m.isupperlimit())
        self.assertEqual(
            get_fromstr(assay.measurement.results[0].isotopes['K-40']),
            '20 +- 10 mBq/kg')
        stored = assay.extra_metadata['radiopurity']
        self.assertEqual(stored['_id'], record.id)
        self.assertEqual(stored['measurement']['results'],
                         record.raw['measurement']['results'])
        self.assertIn('imported', stored)

    def test_skipped_values(self):
        records, _ = parse('copper')
        records = byid(records)
        # every value has units that can't be imported
        assay = records['60e5f6bc6fd19adee2f9d51d'].to_assay('main').save()
        self.assertEqual(assay.sources, [])
        self.assertEqual(dict(assay.measurement.results[0].isotopes), {})
        self.assertEqual(
            len(assay.extra_metadata['radiopurity']['measurement']
                ['results']), 8)
        # stored in the results, but not as a source
        assay = records['60b4f3ecae890f84b01b2cbf'].to_assay('main').save()
        self.assertIn('Rb', assay.measurement.results[0].isotopes)
        self.assertNotIn('Rb', [s.name for s in assay.sources])

    def test_duplicate_names(self):
        record = rp.RadiopurityRecord(raw=rp._empty_raw(), values=[
            rp.RadiopurityValue('U', 'U-238', '1 ppb'),
            rp.RadiopurityValue('U-238', 'U-238', '12 mBq/kg')])
        record.raw['sample']['name'] = 's'
        assay = record.to_assay('main').save()
        self.assertEqual(set(assay.measurement.results[0].isotopes),
                         {'U-238', 'U-238 (2)'})

    def test_all_fixtures_valid(self):
        for name in ('mumetal', 'copper', 'shapes'):
            for record in parse(name)[0]:
                with self.subTest(fixture=name, id=record.id):
                    record.to_assay('main').validate()

    def test_date(self):
        records, _ = parse('mumetal')
        dated = [r for r in records if r.raw['measurement'].get('date')]
        self.assertEqual(len(dated), 1)
        assay = dated[0].to_assay('main')
        self.assertEqual(assay.measurement.date_measured.isoformat(),
                         '2024-01-25')
        # an unknown format is kept in the notes
        dated[0].raw['measurement']['date'] = ['Spring 2020']
        assay = dated[0].to_assay('main')
        self.assertIsNone(assay.measurement.date_measured)
        self.assertIn('Measured Spring 2020', assay.measurement.notes)
