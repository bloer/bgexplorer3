""" Unit mismatches are caught when data is saved or audited, not when results
are calculated
"""
from mongoengine import ValidationError
from bgexplorer.models.budget import dashboard
from bgexplorer.models.common import UnitMismatchError
from bgexplorer.models.component import Component
from bgexplorer.models.emissionspec import EmissionSpec, EmissionSource
from bgexplorer.models.hiteff import HitEfficiency, NormMultiplier
from bgexplorer.models.settings import get_settings, HitEffDbConfig
from bgexplorer.models.sourceterm import (SourceTerm, check_version_units,
                                          unit_checks_deferred)
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase

# value times norm is in dru for both, so that the display unit accepts both,
# but only RATE fits the emission rate of mBq/kg
RATE = dict(v1='0.1 +- 0.01 dru/mBq')
FLUX = dict(v1='0.1 +- 0.01 dru*s*cm**2')


class TestUnitChecks(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')
        self.spec = EmissionSpec(name='e1', sources=[
            EmissionSource(name='Th232', rate='10 +- 1 mBq/kg')]).save()

    def flux_hiteff(self, location, **kwargs):
        return HitEfficiency(source='Th232', location=location,
                             norm=NormMultiplier.flux, scalars=FLUX, **kwargs)

    def component(self, location='c1'):
        return Component(name=f'comp {location}', mass='2 kg',
                         location=location, specs=[self.spec])

    def test_sourceterm_rejects_mismatched_hiteff(self):
        """ A rate in mBq doesn't fit a hit efficiency normalized per area """
        self.flux_hiteff('c1').save()
        with self.assertRaisesRegex(ValidationError, 'mBq'):
            self.component().save()
        self.assertEqual(SourceTerm.objects.count(), 0)

    def test_hiteff_rejects_mismatched_sourceterms(self):
        """ The hit efficiency is rejected, rather than every calculation of
        the terms it would apply to
        """
        HitEfficiency(source='Th232', location='c9', norm=NormMultiplier.flux,
                      scalars=FLUX).save()
        self.component().save()
        self.assertEqual(SourceTerm.objects.count(), 1)
        with self.assertRaisesRegex(ValidationError, 'normalized per'):
            self.flux_hiteff('c1').save()
        self.assertEqual(HitEfficiency.objects(location='c1').count(), 0)
        self.assertEqual(SourceTerm.objects.get().hiteffs, [])
        # the same norm as the term's rate is fine
        HitEfficiency(source='Th232', location='c1', scalars=RATE).save()
        self.assertEqual(len(SourceTerm.objects.get().hiteffs), 1)

    def test_hiteff_without_sourceterms(self):
        """ Nothing to check against the terms, but the units still have to
        fit the settings
        """
        HitEfficiency(source='Th232', location='c9', scalars=RATE).save()
        with self.assertRaises(ValidationError):
            HitEfficiency(source='U238', location='c9',
                          scalars=dict(v1='1 kg')).save()

    def test_audit(self):
        """ Data written without validating is found afterwards """
        self.component().save()
        good = HitEfficiency(source='Th232', location='c1', scalars=RATE).save()
        self.assertEqual(check_version_units('main'), [])
        with unit_checks_deferred():
            bad = self.flux_hiteff('c1').save()
        self.assertEqual(len(SourceTerm.objects.get().hiteffs), 2)
        problems = check_version_units('main')
        self.assertEqual(len(problems), 1)
        self.assertIn(str(bad.id), problems[0])
        self.assertNotIn(str(good.id), problems[0])
        with self.assertRaises(UnitMismatchError):
            dashboard(Component.objects.get(), 'v1')

    def test_audit_adds_display_units(self):
        """ Everything saved gets a display unit, even if the settings were
        replaced
        """
        self.component().save()
        HitEfficiency(source='Th232', location='c1', scalars=RATE).save()
        settings = get_settings('main')
        unit = settings.hiteffdbconfig.scalar_unit('v1')
        self.assertIsNotNone(unit)
        settings.update(set__hiteffdbconfig=HitEffDbConfig())
        self.assertIsNone(get_settings('main').hiteffdbconfig.scalar_unit('v1'))
        self.assertEqual(check_version_units('main'), [])
        self.assertEqual(get_settings('main').hiteffdbconfig.scalar_unit('v1'),
                         unit)

    def test_deferred(self):
        """ Imports and merges can write mismatched terms, to be reported """
        self.flux_hiteff('c1').save()
        with unit_checks_deferred():
            self.component().save()
        self.assertEqual(SourceTerm.objects.count(), 1)
        self.assertEqual(len(check_version_units('main')), 1)
        with self.assertRaises(ValidationError):
            SourceTerm.objects.get().save()

    def test_page_reports_mismatch(self):
        """ Saved data that is still mismatched is reported, not an
        unexplained error
        """
        self.flux_hiteff('c1').save()
        with unit_checks_deferred():
            comp = self.component().save()
        response = self.client.get(self.url('component.results', object=comp))
        self.assertEqual(response.status_code, 500)
        self.assertIn("Units in the saved data don't match",
                      self.html(response))
        response = self.client.get(self.url('component.dashboard_json',
                                            object=comp))
        self.assertEqual(response.status_code, 400)
        self.assertIn('normalized per', response.get_json()['error']['message'])
