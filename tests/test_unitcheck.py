""" Units are checked once, when emission sources, components and hit
efficiencies are saved, and SourceTerms only match hit efficiencies whose
norm fits their source's multiplier. Data written without validating is found
by the audit after an import or merge.
"""
import numpy as np
from mongoengine import ValidationError
from bgexplorer.models.asymmetric import AsymmetricUncertainty
from bgexplorer.models.budget import dashboard
from bgexplorer.models.common import units, UnitMismatchError
from bgexplorer.models.histogram import Histogram
from bgexplorer.models.component import Component
from bgexplorer.models.emissionspec import (EmissionSpec, EmissionSource,
                                            Multiplier)
from bgexplorer.models.hiteff import HitEfficiency, NormMultiplier
from bgexplorer.models.isotope import concentration_to_rate
from bgexplorer.models.settings import (get_settings, HitEffDbConfig,
                                        SpectrumROI)
from bgexplorer.models.sourceterm import (SourceTerm, check_version_units,
                                          load_hiteffs)
from bgexplorer.models.termcalc import TermCalculator
from bgexplorer.models import versioncontrol as vc
from tests.test_app_components import AppTestCase

# value times norm is in dru for each
RATE = dict(v1='0.1 +- 0.01 dru/mBq')
FLUX = dict(v1='0.1 +- 0.01 dru*s*cm**2')
FLUX_SR = dict(v1='0.1 +- 0.01 dru*s*cm**2*sr')
ABSOLUTE = dict(v1='0.1 +- 0.01 dru')


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

    def test_rate_required(self):
        for name in ('U238', 'cosmics'):
            with self.subTest(name=name):
                self.assertRejected(self.source(None, name=name), 'rate')

    def test_absolute(self):
        """ An absolute rate is a dimensionless scale factor, which must be
        chosen explicitly, and isn't a mass of an isotope
        """
        for rate in ('1 Bq', '1 1/cm**2/s', '1 ppb/kg'):
            with self.subTest(rate=rate):
                self.assertRejected(self.source(rate, name='cosmics',
                                                multiplier=Multiplier.absolute),
                                    'invalid for multiplier')
        self.assertRejected(self.source('1 ug', multiplier=Multiplier.absolute),
                            'invalid for multiplier')
        self.assertRejected(self.source('1', name='cosmics'),
                            'absolute multiplier')

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
                              (NormMultiplier.rate, FLUX),
                              (NormMultiplier.absolute, RATE),
                              (NormMultiplier.rate, ABSOLUTE)):
            with self.subTest(norm=norm, scalars=scalars):
                with self.assertRaisesRegex(ValidationError,
                                            'has incorrect units'):
                    HitEfficiency(source='U238', location='c2', norm=norm,
                                  scalars=scalars).save()
        self.assertEqual(HitEfficiency.objects.count(), 1)

    def test_hiteff_norm_required(self):
        """ There is always a norm, by default the rate """
        hiteff = HitEfficiency(source='U238', location='c1', norm=None,
                               scalars=RATE).save()
        self.assertIs(hiteff.reload().norm, NormMultiplier.rate)
        with self.assertRaisesRegex(ValidationError, 'norm'):
            HitEfficiency(source='U238', location='c1', norm='none',
                          scalars=RATE).save()


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

    def test_absolute(self):
        """ A scale factor for an absolutely normalized hit efficiency, even
        for an isotope's name
        """
        for name, rate in (('cosmics', '1'), ('cosmics', '0.5 +- 0.1'),
                           ('U238', '1'), ('cosmics', '50 percent')):
            with self.subTest(name=name, rate=rate):
                EmissionSpec.drop_collection()
                source = self.saved(self.source(
                    rate, name=name, multiplier=Multiplier.absolute))
                self.assertEqual(source.multiplier, Multiplier.absolute)
                self.assertEqual(source.standardized_rate, source.rate)

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


class TestHitEfficiencyMatching(UnitTestCase):
    """ SourceTerms only match hit efficiencies whose norm fits their
    source's multiplier
    """
    def setUp(self):
        super().setUp()
        for norm, scalars in ((NormMultiplier.rate, RATE),
                              (NormMultiplier.flux, FLUX),
                              (NormMultiplier.flux_per_sr, FLUX_SR),
                              (NormMultiplier.absolute, ABSOLUTE)):
            HitEfficiency(source='U238', location='c1', norm=norm,
                          scalars=scalars).save()

    def term(self, rate, multiplier=None, location='c1', **kwargs):
        spec = EmissionSpec(name='e', sources=[
            self.source(rate, multiplier=multiplier)]).save()
        self.component = Component(name='c', location=location, specs=[spec],
                                   **kwargs).save()
        # not the U235 and Ra226 generated from it
        return SourceTerm.objects.get(source__name='U238')

    def norms(self, term):
        return {h.norm for h in term.reload().hiteffs}

    def value(self, term):
        """ The term's v1 in dru """
        config = get_settings('main').hiteffdbconfig
        values = TermCalculator(config, load_hiteffs([term])).scalars(term)
        unit = config.scalar_unit('v1')
        return (values['v1'] * unit).to('dru').m.mode

    def assertMatches(self, term, norms, value=None):
        self.assertEqual(self.norms(term), set(norms))
        self.assertEqual(self.norms(term),
                         {h.norm for h in term.hiteffs_query})
        if value is not None:
            self.assertAlmostEqual(self.value(term), value)

    def test_mass(self):
        term = self.term('10 mBq/kg', mass='2 kg')
        self.assertMatches(term, [NormMultiplier.rate], 0.1 * 10 * 2)

    def test_concentration(self):
        term = self.term('10 ppb', mass='2 kg')
        rate = concentration_to_rate('U238', 10e-9).to('mBq/kg').m
        self.assertMatches(term, [NormMultiplier.rate], 0.1 * rate * 2)

    def test_isotope_mass_per_piece(self):
        term = self.term('1 ug')
        rate = concentration_to_rate('U238', units('1 ug')).to('mBq').m
        self.assertMatches(term, [NormMultiplier.rate], 0.1 * rate)

    def test_inner_surface(self):
        term = self.term('10 mBq/m**2', Multiplier.inner_surface,
                         inner_surface_area='3 m**2',
                         outer_surface_area='5 m**2')
        self.assertMatches(term, [NormMultiplier.rate], 0.1 * 10 * 3)

    def test_flux(self):
        term = self.term('2 1/cm**2/s', Multiplier.flux, mass='2 kg')
        self.assertMatches(term, [NormMultiplier.flux], 0.1 * 2)

    def test_flux_per_sr(self):
        term = self.term('2 1/cm**2/s/sr', Multiplier.flux_per_sr)
        self.assertMatches(term, [NormMultiplier.flux_per_sr], 0.1 * 2)

    def test_flux_without_multiplier(self):
        """ is taken as a surface rate """
        term = self.term('2 1/cm**2/s', outer_surface_area='1 cm**2')
        self.assertEqual(term.source.multiplier, Multiplier.surface)
        self.assertMatches(term, [NormMultiplier.rate], 0.1 * 2e3)

    def test_absolute(self):
        """ An absolutely normalized hit efficiency is only scaled by the
        rate and the placement weights, not the component's mass
        """
        term = self.term('0.5 +- 0.1', Multiplier.absolute, mass='2 kg')
        self.assertMatches(term, [NormMultiplier.absolute], 0.1 * 0.5)
        self.assertIsNone(term.livetimes[0])

    def test_absolute_cosmics(self):
        """ e.g. cosmic rays on a component of their own, whose source isn't
        an isotope
        """
        HitEfficiency(source='cosmics', location='detector',
                      norm=NormMultiplier.absolute, scalars=ABSOLUTE,
                      nprimaries=1000).save()
        spec = EmissionSpec(name='cosmics', sources=[
            self.source('1', name='cosmics',
                        multiplier=Multiplier.absolute)]).save()
        Component(name='cosmics', location='detector', specs=[spec]).save()
        term = SourceTerm.objects.get()
        self.assertMatches(term, [NormMultiplier.absolute], 0.1)
        # no rate to convert primaries to a livetime
        self.assertEqual(term.livetimes, [None])

    def test_new_hiteff(self):
        """ A hit efficiency saved after the term is only added to it if its
        norm fits
        """
        term = self.term('10 mBq/kg', location='c2', mass='2 kg')
        self.assertMatches(term, [])
        HitEfficiency(source='U238', location='c2', norm=NormMultiplier.flux,
                      scalars=FLUX).save()
        self.assertMatches(term, [])
        rate = HitEfficiency(source='U238', location='c2',
                             scalars=RATE).save()
        self.assertMatches(term, [NormMultiplier.rate], 2)
        # changing the norm removes it
        rate.norm = NormMultiplier.flux
        rate.scalars = {k: HitEfficiency.scalars.field.to_python(v)
                        for k, v in FLUX.items()}
        rate.save()
        self.assertMatches(term, [])

    def test_change_multiplier(self):
        """ Changing a source's multiplier replaces its hit efficiencies,
        even with none
        """
        term = self.term('10 mBq/m**2', outer_surface_area='1 m**2')
        self.assertMatches(term, [NormMultiplier.rate], 1)
        spec = EmissionSpec.objects.get()
        spec.sources[0].multiplier = Multiplier.flux
        spec.sources[0].rate = '2 1/cm**2/s'
        spec.save()
        self.assertMatches(term, [NormMultiplier.flux], 0.2)
        spec.sources[0].multiplier = Multiplier.absolute
        spec.sources[0].rate = '2'
        spec.save()
        self.assertMatches(term, [NormMultiplier.absolute], 0.2)
        spec.sources[0].multiplier = Multiplier.mass
        spec.sources[0].rate = '1 mBq/kg'
        spec.save()
        # the component has no mass, so nothing in the results
        self.assertMatches(term, [NormMultiplier.rate], 0)
        HitEfficiency.objects(norm=NormMultiplier.rate).delete()
        spec.save()
        self.assertMatches(term, [])


class TestAudit(UnitTestCase):
    """ Data written without validating, e.g. by an import or merge, is found
    by `check_version_units`, and calculating it is an error rather than a
    wrong result
    """
    def setUp(self):
        super().setUp()
        spec = EmissionSpec(name='e', sources=[
            self.source('10 mBq/kg')]).save()
        self.comp = Component(name='c', mass='2 kg', location='c1',
                              specs=[spec]).save()
        self.good = HitEfficiency(source='U238', location='c1',
                                  scalars=RATE).save()

    def bad_hiteff(self):
        """ A hit efficiency whose v1 doesn't fit the display unit """
        bad = HitEfficiency(source='U238', location='c1', scalars=RATE).save()
        # validation rejects this, so go around it
        bad.scalars['v1'] = units('1 kg')
        bad.save(validate=False)
        return bad

    def test_clean(self):
        self.assertEqual(check_version_units('main'), [])

    def test_hiteff(self):
        bad = self.bad_hiteff()
        term = SourceTerm.objects.get(source__name='U238')
        self.assertEqual(len(term.hiteffs), 2)
        problems = check_version_units('main')
        self.assertEqual(len(problems), 1)
        self.assertIn(str(bad.id), problems[0])
        self.assertNotIn(str(self.good.id), problems[0])
        with self.assertRaisesRegex(UnitMismatchError,
                                    'not the display unit'):
            dashboard(Component.objects.get(), 'v1')

    def test_source(self):
        spec = EmissionSpec(name='flux', version_tags=['main'], sources=[
            self.source('1 ppb', name='Gammaflux')])
        spec.save(validate=False)
        problems = check_version_units('main')
        self.assertEqual(len(problems), 1)
        self.assertIn('Gammaflux', problems[0])
        self.assertIn('radioactive isotope', problems[0])

    def test_adds_display_units(self):
        """ Everything saved gets a display unit, even if the settings were
        replaced
        """
        settings = get_settings('main')
        unit = settings.hiteffdbconfig.scalar_unit('v1')
        self.assertIsNotNone(unit)
        settings.update(set__hiteffdbconfig=HitEffDbConfig())
        self.assertIsNone(get_settings('main').hiteffdbconfig.scalar_unit('v1'))
        self.assertEqual(check_version_units('main'), [])
        self.assertEqual(get_settings('main').hiteffdbconfig.scalar_unit('v1'),
                         unit)

    def test_page_reports_mismatch(self):
        """ Saved data that is still mismatched is reported, not an
        unexplained error
        """
        self.bad_hiteff()
        response = self.client.get(self.url('component.results',
                                            object=self.comp))
        self.assertEqual(response.status_code, 500)
        self.assertIn("Units in the saved data don't match",
                      self.html(response))
        response = self.client.get(self.url('component.dashboard_json',
                                            object=self.comp))
        self.assertEqual(response.status_code, 400)
        self.assertIn('not the display unit',
                      response.get_json()['error']['message'])

    def test_roi_in_terms(self):
        """ ROIs are results of the terms like scalars, and a scalar with the
        same key replaces the ROI
        """
        settings = get_settings('main')
        settings.hiteffdbconfig.rois = [
            SpectrumROI(spectrum='s1', display_name='roi1', mode='average',
                        start=0*units.keV, stop=5*units.keV),
            SpectrumROI(spectrum='s1', display_name='roi2', mode='average',
                        start=0*units.keV, stop=5*units.keV)]
        settings.save()
        hist = Histogram(AsymmetricUncertainty.fromcounts(np.arange(10))
                         * units('dru/mBq'), np.arange(11) * units.keV)
        HitEfficiency(source='U238', location='c2', spectra=dict(s1=hist),
                      scalars=dict(roi2='1000 +- 1 dru/mBq')).save()
        self.comp.location = 'c2'
        self.comp.save()
        term = SourceTerm.objects.get(source__name='U238')
        config = get_settings('main').hiteffdbconfig
        values = TermCalculator(config, load_hiteffs([term])).scalars(term)
        self.assertEqual(set(values), {'roi1', 'roi2'})
        rate = 20  # mBq
        self.assertAlmostEqual(values['roi2'].mode / rate, 1000)
        self.assertAlmostEqual(values['roi1'].mode / rate, 2, places=1)
