import re
import mendeleev
import math
from typing import Union, Optional
from .common import units

_ln2 = math.log(2)

IsotopeType = Union[str, mendeleev.Isotope]

_isotopestr = re.compile(r'(\d{1,3})?([A-Z][a-z]?[a-z]?)-?(\d{1,3})?')


def get_isotope(source: IsotopeType) -> Optional[mendeleev.Isotope]:
    """ If string represents an isotope, look it up """
    if isinstance(source, mendeleev.Isotope):
        return source
    match = _isotopestr.match(source)
    if match:
        A, symbol, A2 = match.groups()
        A = A or A2
        try:
            return mendeleev.isotope(symbol, A)
        except Exception:
            pass
    return None

def compare_source_names(source1: IsotopeType, source2: IsotopeType) -> bool:
    """ test if `source1` is a match for `source2` including checking
    different formats for entering isotopes
    """
    if source1 == source2:
        return True
    try:
        return get_isotope(source1).id == get_isotope(source2).id
    except AttributeError:
        return False

def get_halflife(source: IsotopeType) -> Optional[units.Quantity]:
    isotope = get_isotope(source)
    if isotope and isotope.half_life:
        unitstr = isotope.half_life_unit.replace('y', 'year')
        return isotope.half_life * units(unitstr)
    return None

def get_tau(source: IsotopeType) -> Optional[units.Quantity]:
    halflife = get_halflife(source)
    if halflife is not None:
        return halflife / _ln2

def concentration_to_rate(source: IsotopeType,
                          concentration: Union[float, units.Quantity],
                          applyabundance: Optional[bool] = None,
                          ) -> units.Quantity:
    """ convert fraction concentration of an isotope (like ppb) to
    fractional dcay rate in mBq/kg

    If applyabundance is true, multiply by the isotope's natural abundance
    if None (default), apply only to K40, which is commonly written rather
    than the more correct Knat
    """
    if source in ('K', 'natK', 'K-nat', 'Knat'):
        source = 'K40'
        applyabundance = True
    isotope = get_isotope(source)
    halflife = get_halflife(source)
    if halflife is None:
        raise ValueError(f"'{source}' is not a valid radioactive isotope")
    if applyabundance is None and compare_source_names(source, 'K40'):
        applyabundance = True
    if applyabundance:
        concentration = concentration * isotope.abundance/100.
    A = isotope.mass * units('g/mol')
    # return will throw invalid unit error if `conc` is not dimensionless
    return (concentration * units.N_A / A / halflife * _ln2)


def rate_to_concentration(source: IsotopeType, rate: units.Quantity,
                          applyabundance: bool = True) -> units.Quantity:
    """ convert decay rate to fractional quantity """
    # TODO: handle surface rates
    if source in ('K', 'natK', 'K-nat', 'Knat'):
        source = 'K40'
        applyabundance = True
    isotope = get_isotope(source)
    halflife = get_halflife(source)
    if halflife is None:
        raise ValueError(f"'{source}' is not a valid radioactive isotope")
    A = isotope.mass * units('g/mol')
    concentration = (rate / units.N_A * A * halflife / _ln2)
    if applyabundance is None and compare_source_names(source, 'K40'):
        applyabundance = True
    if applyabundance:
        concentration = concentration / (isotope.abundance/100.)
    return concentration
