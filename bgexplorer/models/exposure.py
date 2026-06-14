from math import exp
from .emissionspec import EmissionSpec, EmissionSource
from .fields import QuantityField
from .common import units
from .isotope import get_tau

_tau_rn222 = get_tau('Rn222')
_tau_pb210 = get_tau('Pb210')


# this requires EmissionSource to be inheritable...
class RadonExposure(EmissionSpec):
    radonlevel = QuantityField(units='Bq/m**3')
    exposuretime = QuantityField(units='day')
    columnheight = QuantityField(units='cm', default=10*units.cm)
    multiplier = EnumField(Multiplier)
                                                )

    def rate_free(self):
        (self.radonlevel * self.columnheight *
         (1-exp(-self.exposuretime / _tau_pb210))).to('Bq/cm**2')

    def rate_trapped(self):
        R0 = (self.radonlevel * self.columnheight *
              (1-exp(-self.exposuretime/_tau_rn222)) *
              _tau_rn222 / _tau_pb210)
            return R0 * exp(-self.exposuretime/_tau_pb210).to('Bq/cm**2')


    def clean(self):
        super().clean()
        if self.multiplier not in (Multiplier.surface,
                                   Multiplier.inner_surface,
                                   Multiplier.outer_surface):
            self.error("multiplier must be a surface")

    def get_sources(component):
        return [EmissionSource(id=self.id, name='Pb210', category='radon',
                               comment=self.comment,
                               multiplier=self.multiplier,
                               rate=self.rate_free())]


class DustExposure(EmissionSpec):
    exposuretime = QuantityField(units='day')

    def clean(self):
        super().clean()
        # todo: also allow specs like 'pg/cm**2/s'
        for source in self.sources:
            if not validate_unit(source.rate, 'Bq/cm**2/s'):
                self.error("Dust sources must have units Bq/cm**2/s")
            if source.multiplier not in (Multiplier.surface,
                                         Multiplier.inner_surface,
                                         Multiplier.outer_surface):
                self.error("multiplier must be a surface")

    def get_sources(component):
        return [EmissionSource(id=s.id, name=s.name, category='dust',
                               comment=s.comment, multiplier=s.multiplier,
                               rate=s.rate * self.exposuretime,
                               generated_from=s.generated_from)
                for s in self.sources]
