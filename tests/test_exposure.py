import unittest
from math import exp
from mongoengine import disconnect, ValidationError
from bgexplorer.models.component import Component
from bgexplorer.models.emissionspec import (EmissionSpec, Multiplier,
                                            SourceCategory)
from bgexplorer.models.exposure import RadonExposure, RadonPeriod, RadonMode
from bgexplorer.models.isotope import get_tau
from bgexplorer.models.sourceterm import SourceTerm
from bgexplorer.models.common import units
from tests.dbutil import connect_test_db

TAU_PB = get_tau('Pb210')
TAU_RN = get_tau('Rn222')


def bqm2(q):
    return q.to('Bq/m**2').m


def nominal(q):
    m = bqm2(q)
    return getattr(m, 'nominal_value', m)


class TestRadonExposure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect_test_db()

    @classmethod
    def tearDownClass(cls):
        disconnect()

    def setUp(self):
        for cls in (Component, EmissionSpec, SourceTerm):
            cls.drop_collection()

    def assertRate(self, rate, expected, places=12):
        self.assertAlmostEqual(nominal(rate), nominal(expected),
                               places=places)

    def test_free(self):
        spec = RadonExposure(name='r', periods=[RadonPeriod(
            radonlevel='10 +- 1 Bq/m**3', duration='30 day',
            columnheight='20 cm')])
        level = 10 * units('Bq/m**3') * 20 * units.cm
        t = 30 * units.day
        self.assertRate(spec.pb210_rate(),
                        level * (1 - exp(-(t / TAU_PB).to('').m)))
        # for t << tau, every plated out daughter is still there
        self.assertAlmostEqual(nominal(spec.pb210_rate()) /
                               nominal(level * t / TAU_PB), 1, places=2)
        # uncertainty comes from the radon level
        rate = bqm2(spec.pb210_rate())
        self.assertAlmostEqual(rate.s0 / rate.nominal_value, 0.1)

    def test_trapped(self):
        # long enough for all the sealed radon to decay
        spec = RadonExposure(name='r', periods=[RadonPeriod(
            mode=RadonMode.trapped, radonlevel='10 Bq/m**3',
            duration='1000 day', columnheight='5 cm')])
        level = 10 * units('Bq/m**3') * 5 * units.cm
        self.assertRate(spec.pb210_rate(), level * TAU_RN / TAU_PB)

    def test_periods(self):
        """ periods build up in order, each with its own column height """
        first = RadonPeriod(radonlevel='10 Bq/m**3', duration='100 day',
                            columnheight='10 cm')
        second = RadonPeriod(radonlevel='50 Bq/m**3', duration='2000 day',
                             columnheight='30 cm')
        spec = RadonExposure(name='r', periods=[first, second])
        survive = exp(-(2000 * units.day / TAU_PB).to('').m)
        expected = (bqm2(first.pb210_rate()).nominal_value * survive +
                    bqm2(second.pb210_rate()).nominal_value)
        self.assertAlmostEqual(bqm2(spec.pb210_rate()).nominal_value,
                               expected, places=12)
        # the same history in one period gives the same result
        joined = RadonExposure(name='r', periods=[
            RadonPeriod(radonlevel='10 Bq/m**3', duration='50 day'),
            RadonPeriod(radonlevel='10 Bq/m**3', duration='50 day')])
        single = RadonExposure(name='r', periods=[
            RadonPeriod(radonlevel='10 Bq/m**3', duration='100 day')])
        self.assertAlmostEqual(bqm2(joined.pb210_rate()).nominal_value,
                               bqm2(single.pb210_rate()).nominal_value,
                               places=12)

    def test_sources(self):
        spec = RadonExposure(name='r', periods=[RadonPeriod(
            radonlevel='10 Bq/m**3', duration='3 day')]).save()
        self.assertEqual(spec.category, SourceCategory.radon)
        pb210 = [s for s in spec.sources if not s.generated_from]
        self.assertEqual([s.name for s in pb210], ['Pb210'])
        self.assertEqual(pb210[0].multiplier, Multiplier.surface)
        self.assertEqual(pb210[0].category, SourceCategory.radon)
        self.assertRate(pb210[0].rate, spec.pb210_rate())

        # the source keeps its id as the exposure changes
        spec.periods.append(RadonPeriod(radonlevel='100 Bq/m**3',
                                        duration='1 day'))
        spec.multiplier = Multiplier.outer_surface
        spec.save()
        spec.reload()
        source = [s for s in spec.sources if not s.generated_from][0]
        self.assertEqual(source.id, pb210[0].id)
        self.assertEqual(source.multiplier, Multiplier.outer_surface)
        self.assertRate(source.rate, spec.pb210_rate())

    def test_sourceterm(self):
        spec = RadonExposure(name='r', periods=[RadonPeriod(
            radonlevel='10 Bq/m**3', duration='3 day')]).save()
        c1 = Component(name='c1', inner_surface_area='1 m**2',
                       outer_surface_area='2 m**2', specs=[spec]).save()
        st = SourceTerm.objects(assemblyRoot=c1, source__name='Pb210').get()
        self.assertAlmostEqual(st.emissionrate.to('Bq').m.nominal_value,
                               3 * bqm2(spec.pb210_rate()).nominal_value)

    def test_errors(self):
        with self.assertRaises(ValidationError):
            RadonExposure(name='r', multiplier=Multiplier.mass, periods=[
                RadonPeriod(radonlevel='1 Bq/m**3', duration='1 day')]
            ).save()
        # incomplete periods are a validation error, not a crash
        with self.assertRaises(ValidationError):
            RadonExposure(name='r', periods=[RadonPeriod(duration='1 day')]
                          ).save()


if __name__ == '__main__':
    unittest.main()
