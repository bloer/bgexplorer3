from enum import Enum
from math import exp
from bson import ObjectId
from mongoengine import (EmbeddedDocument, EmbeddedDocumentListField,
                         EnumField, ObjectIdField, StringField,
                         ValidationError)
from .emissionspec import (EmissionSpec, EmissionSource, Multiplier,
                           SourceCategory)
from .fields import QuantityField, UncertainQuantityField
from .common import units
from .isotope import get_tau

_tau_rn222 = get_tau('Rn222')
_tau_pb210 = get_tau('Pb210')

SURFACES = (Multiplier.surface, Multiplier.inner_surface,
            Multiplier.outer_surface)


def _decayed(t, tau) -> float:
    """ Fraction of atoms with mean lifetime `tau` that decay within `t` """
    return 1 - exp(-(t / tau).to('').m)


class RadonMode(Enum):
    free = 'free'         # radon is replenished, e.g. open air
    trapped = 'trapped'   # a fixed amount of radon is sealed in, and decays


class RadonPeriod(EmbeddedDocument):
    """ A time a surface spent exposed to air with some radon level """
    id = ObjectIdField(required=True, default=ObjectId)
    description = StringField(help_text="e.g. cleanroom, lab")
    mode = EnumField(RadonMode, default=RadonMode.free,
                     help_text="free: the radon is replenished; trapped: a "
                               "fixed amount is sealed in and decays")
    radonlevel = UncertainQuantityField(required=True, units='Bq/m**3',
                                        label="Radon level")
    duration = QuantityField(required=True, units='day')
    columnheight = QuantityField(
        units='cm', default=10*units.cm, label="Column height",
        help_text="Height of air whose radon daughters plate out")

    def pb210_rate(self) -> units.Quantity:
        """ Pb210 surface activity deposited by the end of the period """
        deposited = self.radonlevel * self.columnheight
        if self.mode is RadonMode.trapped:
            # every decay of the sealed radon deposits one Pb210 atom
            atoms = deposited * _tau_rn222 * _decayed(self.duration,
                                                      _tau_rn222)
            return (atoms / _tau_pb210).to('Bq/m**2')
        # Pb210 builds up towards equilibrium with the deposition rate
        return (deposited * _decayed(self.duration, _tau_pb210)
                ).to('Bq/m**2')


class RadonExposure(EmissionSpec):
    """ Pb210 on a surface from radon daughters plating out, calculated from
    the surface's history of exposure to radon. The periods are in order,
    and gaps between them are ignored.

    If the surface activity is already known, use a plain EmissionSpec with
    category radon instead.
    """
    COMPUTED_SOURCES = True
    periods = EmbeddedDocumentListField(RadonPeriod)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('category', SourceCategory.radon)
        kwargs.setdefault('multiplier', Multiplier.surface)
        super().__init__(*args, **kwargs)

    def pb210_rate(self) -> units.Quantity:
        """ Pb210 surface activity at the end of the last period """
        rate = None
        for period in self.periods:
            # earlier deposits decay during this period
            if rate is not None:
                rate = rate * (1 - _decayed(period.duration, _tau_pb210))
            rate = period.pb210_rate() + (0 if rate is None else rate)
        return rate

    def clean(self):
        if self.multiplier not in SURFACES:
            raise ValidationError("Radon plates out on a surface",
                                  field_name='multiplier')
        # keep the id so correlations and anything keyed to it stay the same
        old = [s for s in self.sources if not s.generated_from]
        sources = [s for s in self.sources if s.generated_from]
        # incomplete periods are reported by field validation
        if self.periods and all(p.radonlevel is not None and
                                p.duration is not None and
                                p.columnheight is not None
                                for p in self.periods):
            total = sum((p.duration for p in self.periods), 0 * units.day)
            source = EmissionSource(
                name='Pb210', category=SourceCategory.radon,
                multiplier=self.multiplier, rate=self.pb210_rate(),
                comment=f"Radon plate-out over {len(self.periods)} "
                        f"period(s), {total:~.3g} total")
            if old:
                source.id = old[0].id
            sources.insert(0, source)
        self.sources = sources
        super().clean()
