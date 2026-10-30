""" Units are checked once, when emission sources, components and hit
efficiencies are saved, and SourceTerms only match hit efficiencies whose
norm fits their source's multiplier. Data written without validating is found
by the audit after an import or merge.
"""
from mongoengine import ValidationError
from bgexplorer.models.common import units
from bgexplorer.models.component import Component
from bgexplorer.models.emissionspec import (EmissionSpec, EmissionSource,
                                            Multiplier)
from bgexplorer.models.hiteff import HitEfficiency, NormMultiplier
from bgexplorer.models.isotope import concentration_to_rate
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase

# value times norm is in dru for each
RATE = dict(v1='0.1 +- 0.01 dru/mBq')
FLUX = dict(v1='0.1 +- 0.01 dru*s*cm**2')
FLUX_SR = dict(v1='0.1 +- 0.01 dru*s*cm**2*sr')


class UnitTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        vc.create_version('main')

    def source(self, rate, name='U238', multiplier=None):
        return EmissionSource(name=name, rate=rate, multiplier=multiplier)


class TestRejectedAtClean(UnitTestCase):
    def assertRejected(self, source, regex=None):
        spec = EmissionSpec(name='e', sources=[source])
        if regex:
            with self.assertRaisesRegex(ValidationError, regex):
                spec.save()
        else:
            with self.assertRaises(ValidationError):
                spec.save()
        self.assertEqual(EmissionSpec.objects.count(), 0)

    def test_rate_doesnt_fit_multiplier(self):
        for rate, mult in (('1 Bq/kg', Multiplier.surface),
                           ('1 Bq', Multiplier.mass),
                           ('1 Bq/m**3', Multiplier.length),
                           ('1 1/cm**2/s', Multiplier.mass),
                           ('1 ppb', Multiplier.flux),
                           ('1 ug', Multiplier.mass)):
            with self.subTest(rate=rate, multiplier=mult):
                self.assertRejected(self.source(rate, multiplier=mult),
                                    'invalid for multiplier')

    def test_unrecognized_units(self):
        for rate in ('1 1/s/kg**2', '1 m', '1 Bq*kg'):
            with self.subTest(rate=rate):
                self.assertRejected(self.source(rate), 'Unhandled rate units')

    def test_mass_needs_radioactive_isotope(self):
        """ A mass is only converted to a decay rate for a radioactive
        isotope, not e.g. a flux or a stable isotope
        """
        for name in ('Gammaflux', 'Pb206'):
            for rate in ('1 ppb', '1e-9', '1 g/m**2', '1 ug'):
                with self.subTest(name=name, rate=rate):
                    self.assertRejected(self.source(rate, name=name),
                                        'radioactive isotope')

    def test_spec_multiplier_doesnt_fit(self):
        """ The spec's multiplier is used for sources without one, so must
        fit them
        """
        spec = EmissionSpec(name='e', multiplier=Multiplier.inner_surface,
                            sources=[self.source('1 Bq/m**2'),
                                     self.source('1 Bq/kg', name='K40')])
        with self.assertRaisesRegex(ValidationError, 'invalid for multiplier'):
            spec.save()

    def test_component_units(self):
        for field, value in (('mass', '2 m'), ('volume', '1 kg'),
                             ('inner_surface_area', '1 m'),
                             ('length', '1 m**2')):
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    Component(name='c', **{field: value}).save()
        self.assertEqual(Component.objects.count(), 0)

    def test_hiteff_display_units(self):
        """ The first hit efficiency sets the display unit, which others must
        fit once multiplied by their norm. Steradians are dimensionless, so
        flux and flux_per_sr can't be told apart
        """
        HitEfficiency(source='U238', location='c1', scalars=RATE).save()
        for norm, scalars in ((NormMultiplier.rate, dict(v1='1 kg')),
                              (NormMultiplier.flux, RATE),
                              (NormMultiplier.rate, FLUX)):
            with self.subTest(norm=norm, scalars=scalars):
                with self.assertRaisesRegex(ValidationError,
                                            'has incorrect units'):
                    HitEfficiency(source='U238', location='c2', norm=norm,
                                  scalars=scalars).save()
        self.assertEqual(HitEfficiency.objects.count(), 1)


class TestAcceptedAtClean(UnitTestCase):
    def saved(self, source):
        spec = EmissionSpec(name='e', sources=[source]).save()
        return spec.reload().sources[0]

    def test_rates(self):
        for rate, mult in (('1 mBq/kg', Multiplier.mass),
                           ('1 uBq/cm**3', Multiplier.volume),
                           ('1 Bq/m**2', Multiplier.surface),
                           ('1 Bq/cm', Multiplier.length),
                           ('1 mBq', Multiplier.per_piece),
                           ('1 1/s', Multiplier.per_piece),
                           # fluxes have the dimensions of surface rates, so
                           # need their multiplier set
                           ('7 1/cm**2/s', Multiplier.surface)):
            with self.subTest(rate=rate):
                EmissionSpec.drop_collection()
                source = self.saved(self.source(rate))
                self.assertEqual(source.multiplier, mult)
                self.assertEqual(source.standardized_rate, source.rate)

    def test_isotope_masses(self):
        """ A mass of a radioactive isotope, e.g. a concentration, is
        converted to a decay rate
        """
        for name, rate, mult in (('U238', '10 ppb', Multiplier.mass),
                                 ('U238', '1e-9', Multiplier.mass),
                                 ('K', '0.4 ppb', Multiplier.mass),
                                 ('Th232', '1 ug/m**2', Multiplier.surface),
                                 ('Th232', '1 ug/cm**3', Multiplier.volume),
                                 ('Th232', '1 ug/m', Multiplier.length),
                                 ('Th232', '1 ug', Multiplier.per_piece)):
            with self.subTest(name=name, rate=rate):
                EmissionSpec.drop_collection()
                source = self.saved(self.source(rate, name=name))
                self.assertEqual(source.multiplier, mult)
                expected = concentration_to_rate(name, source.rate)
                standardized = source.standardized_rate
                self.assertTrue(mult.valid_units.is_compatible_with(
                    standardized.u))
                self.assertAlmostEqual(
                    standardized.to(mult.valid_units).m.mode,
                    expected.to(mult.valid_units).m.mode)

    def test_no_rate(self):
        source = self.saved(self.source(None))
        self.assertIsNone(source.multiplier)
        self.assertIsNone(source.standardized_rate)

    def test_explicit_flux(self):
        for rate, mult in (('7 1/cm**2/s', Multiplier.flux),
                           ('7 1/m**2/day', Multiplier.flux),
                           ('7 1/cm**2/s/sr', Multiplier.flux_per_sr)):
            with self.subTest(rate=rate):
                EmissionSpec.drop_collection()
                source = self.saved(self.source(rate, name='Gammaflux',
                                                multiplier=mult))
                self.assertEqual(source.multiplier, mult)

    def test_spec_multiplier(self):
        """ The spec's multiplier is used for sources without their own """
        spec = EmissionSpec(name='e', multiplier=Multiplier.flux, sources=[
            self.source('7 1/cm**2/s', name='Gammaflux'),
            self.source('1 Bq/kg', name='K40', multiplier=Multiplier.mass)])
        spec.save()
        self.assertEqual([s.multiplier for s in spec.reload().sources],
                         [Multiplier.flux, Multiplier.mass])
