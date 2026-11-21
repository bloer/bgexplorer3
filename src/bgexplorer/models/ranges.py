""" Parse and combine lists of numeric ranges, such as `10-15, 18-30 keV` """
import re
from typing import List, Optional, Sequence, Tuple
from pint.errors import PintError
from .common import units as unitreg

_NUM = r'[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?'
_UNIT = r'[A-Za-zµμ][\w/*^.µμ]*'
_SEP = r'(?:-|–|—|\bto\b)'
_RANGE = re.compile(
    rf'\s*(?P<lo>{_NUM})\s*(?P<ulo>{_UNIT})?\s*{_SEP}\s*'
    rf'(?P<hi>{_NUM})\s*(?P<uhi>{_UNIT})?\s*')


def _quantity(value: float, unit: Optional[str], piece: str):
    try:
        return unitreg.Quantity(value, unit)
    except (PintError, AttributeError, TypeError, ValueError) as e:
        raise ValueError(f"unknown unit '{unit}' in '{piece}'") from e


def parse_ranges(text: str, default_unit=None) -> List[Tuple]:
    """ The (low, high) Quantities in a comma or semicolon separated list like
    "10-15, 18-30 keV". A range without a unit has that of the next one that
    has one, or `default_unit` if none do. "to" can replace the "-", and a
    range can have a unit at each end, as in "500 eV-2 keV". Ranges are
    returned as given, not sorted or merged. Raises ValueError for text that
    can't be understood.
    """
    pieces = [p for p in re.split(r'[;,]', text or '') if p.strip()]
    carry = None if default_unit is None else str(default_unit)
    ranges = []
    for piece in reversed(pieces):
        match = _RANGE.fullmatch(piece)
        if match is None:
            raise ValueError(f"can't read '{piece.strip()}' as a range; "
                             "expected something like '10-15, 18-30 keV'")
        ulo, uhi = match['ulo'], match['uhi']
        unit_hi = uhi or ulo or carry
        unit_lo = ulo or unit_hi
        if unit_hi is None:
            raise ValueError(f"no unit given for '{piece.strip()}'")
        if uhi or ulo:
            carry = unit_hi
        low = _quantity(float(match['lo']), unit_lo, piece.strip())
        high = _quantity(float(match['hi']), unit_hi, piece.strip())
        ranges.append((low, high))
    ranges.reverse()
    return ranges


def merge_ranges(ranges: Sequence[Tuple[float, float]]
                 ) -> List[Tuple[float, float]]:
    """ `ranges`, sorted, with overlapping and touching ones joined. Pairs
    with high <= low are dropped.
    """
    merged = []
    for low, high in sorted(r for r in ranges if r[0] < r[1]):
        if merged and low <= merged[-1][1]:
            if high > merged[-1][1]:
                merged[-1] = (merged[-1][0], high)
        else:
            merged.append((low, high))
    return merged
