""" Search radiopurity.org and convert the results to Assays.

radiopurity.org has no API, so we scrape the results page of its simple
search. The page is rendered by
https://github.com/pnnl/Radiopurity-database-assistant (branch
radiopurity-dot-org, user_interface/templates/simple_search.html)
"""
import re
import datetime
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup
from mongoengine import ValidationError

from .assay import (Assay, SampleInfo, MeasurementInfo, MeasurementRequest,
                    MeasurementResult)
from .common import PublicationInfo
from .emissionspec import EmissionSource, Multiplier
from .fields import UncertainQuantityField
from .isotope import get_isotope

# the bare radiopurity.org certificate doesn't match its host name
SEARCH_URL = 'https://www.radiopurity.org/simple_search'

# radiopurity.org uses the element for the head of its decay chain
SHORTHAND = {'U': 'U-238', 'Th': 'Th-232', 'K': 'K-40'}

_isotope_name = re.compile(
    r'(\d{1,3})?\s*-?\s*([A-Za-z]{1,3})\s*-?\s*(\d{1,3})?')
_num_records = re.compile(r'num records:\s*(\d+)')
_rate_field = UncertainQuantityField()


class RadiopurityError(Exception):
    """ The search couldn't be made, or radiopurity.org reported an error """


@dataclass
class RadiopurityValue:
    """ One entry of a record's measurement results """
    isotope: str             # as given by radiopurity.org
    name: str                # normalized isotope name
    rate: Optional[str]      # a string QuantityField can read, or None
    warning: str = ''
    # False if the value can be stored, but not used as an EmissionSource
    source: bool = True

    def __str__(self):
        return f"{self.name}: {self.rate}"


@dataclass
class RadiopurityRecord:
    """ One search result, as a nested dict with the keys radiopurity.org
    uses in its database, plus the values converted for bgexplorer
    """
    raw: dict
    values: list = field(default_factory=list)

    @property
    def id(self):
        return self.raw.get('_id', '')

    @property
    def name(self):
        sample = self.raw['sample']
        return (sample.get('name')
                or ' '.join(filter(None, [self.raw.get('grouping'),
                                          sample.get('id')]))
                or self.id)

    @property
    def warnings(self):
        return [f"{v.isotope}: {v.warning}" for v in self.values
                if v.warning]

    def to_assay(self, version_tag=None) -> Assay:
        raw = self.raw
        sample, meas = raw['sample'], raw['measurement']
        data_source = raw['data_source']
        notes = [meas.get('description', '')]
        date_measured = _parse_date(meas.get('date', []))
        if meas.get('date') and date_measured is None:
            notes.append("Measured " + ' - '.join(meas['date']))

        valid = [v for v in self.values if v.rate is not None]
        isotopes = {}
        for v in valid:
            key, n = v.name, 1
            while key in isotopes:
                n += 1
                key = f"{v.name} ({n})"
            isotopes[key] = v.rate

        return Assay(
            version_tag=version_tag,
            name=self.name,
            description=sample.get('description', ''),
            radiopurityid=self.id,
            sample=SampleInfo(
                name=sample.get('name', ''),
                id=sample.get('id', ''),
                description=sample.get('description', ''),
                vendor=sample.get('source', ''),
                owner=sample['owner'].get('name', ''),
                ownercontact=sample['owner'].get('contact', ''),
            ),
            measurement=MeasurementInfo(
                technique=meas.get('technique', ''),
                institution=meas.get('institution', ''),
                operator=meas['practitioner'].get('name', ''),
                operatorcontact=meas['practitioner'].get('contact', ''),
                date_measured=date_measured,
                notes='\n'.join(filter(None, notes)),
                results=[MeasurementResult(isotopes=isotopes)],
            ),
            request=MeasurementRequest(
                requestor=meas['requestor'].get('name', ''),
                requestorcontact=meas['requestor'].get('contact', ''),
            ),
            publication=PublicationInfo(
                reference=data_source.get('reference', ''),
                org=raw.get('grouping', ''),
            ),
            sources=[EmissionSource(name=v.name, rate=v.rate)
                     for v in valid if v.source],
            extra_metadata={'radiopurity': {
                **raw,
                'imported': datetime.date.today().isoformat(),
            }},
        )


def search(term: str, include_synonyms: bool = True,
           timeout: float = 60) -> tuple[list, list]:
    """ Search radiopurity.org for `term`. Return (records, warnings) as for
    `parse_results`
    """
    data = dict(query_field='all', comparison_operator='contains',
                query_value=term)
    if include_synonyms:
        data['include_synonyms'] = 'true'
    try:
        response = requests.post(SEARCH_URL, data=data, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as e:
        raise RadiopurityError(f"Could not search radiopurity.org: {e}") \
            from e
    return parse_results(response.text)


def parse_results(html: str) -> tuple[list, list]:
    """ Read the records from a simple_search results page. Return
    (records, warnings), where warnings are problems with the page as a whole
    """
    soup = BeautifulSoup(html, 'html.parser')
    for p in soup.find_all('p', class_='normal-text'):
        if (text := p.get_text(strip=True)).startswith('query error:'):
            raise RadiopurityError(f"radiopurity.org reported a {text}")

    container = soup.find(id='query-results-container')
    if container is None:
        raise RadiopurityError("radiopurity.org returned an unexpected page")
    records = [_parse_record(div) for div in
               container.find_all('div', class_='collapsible-content')]

    warnings = []
    if match := _num_records.search(container.get_text()):
        if int(match.group(1)) != len(records):
            warnings.append(
                f"radiopurity.org reports {match.group(1)} records, but"
                f" only {len(records)} could be read. Its page format may"
                " have changed")
    elif records:
        warnings.append("radiopurity.org didn't report the number of"
                        " records. Its page format may have changed")
    return records, warnings


def _empty_raw():
    person = lambda: dict(name='', contact='')  # noqa: E731
    return {
        '_id': '', 'grouping': '',
        'sample': {'owner': person()},
        'measurement': {'requestor': person(), 'practitioner': person(),
                        'results': []},
        'data_source': {'reference': '', 'input': person()},
    }


def _parse_record(div) -> RadiopurityRecord:
    raw = _empty_raw()
    sections = {
        'sample info': raw['sample'],
        'measurement info': raw['measurement'],
        'measurement requestor': raw['measurement']['requestor'],
        'measurement practitioner': raw['measurement']['practitioner'],
        'data input': raw['data_source']['input'],
        'sample owner': raw['sample']['owner'],
    }
    toplevel = {
        'database id': (raw, '_id'),
        'grouping': (raw, 'grouping'),
        'data reference (publication)': (raw['data_source'], 'reference'),
    }
    values = []
    section = {}
    # html.parser doesn't add tbody, so rows are the tables' children
    for table in div.find_all('table', recursive=False):
        for tr in table.find_all('tr', recursive=False):
            results = tr.find('table', class_='measurement-results-table')
            if results is not None:
                for row in results.find_all('tr'):
                    cells = [p.get_text(strip=True) for p in
                             row.find_all('p', class_='collapsible-value')]
                    cells = [c for c in cells if c]
                    if cells:
                        result, value = _parse_result(cells)
                        raw['measurement']['results'].append(result)
                        values.append(value)
                continue
            cells = [td.get_text(strip=True) for td in
                     tr.find_all('td', recursive=False)]
            if len(cells) == 1:
                section = sections.get(cells[0], {})
            elif len(cells) == 2 and (label := cells[0].rstrip(':')) \
                    in toplevel:
                dest, key = toplevel[label]
                dest[key] = cells[1]
            elif len(cells) == 3:
                label, value = cells[1].rstrip(':'), cells[2]
                if label.endswith('date range'):
                    section['date'] = value.split(' - ', 1)
                elif label.endswith('date'):
                    section['date'] = [value]
                else:
                    section[label] = value
    return RadiopurityRecord(raw=raw, values=values)


def _isfloat(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def _parse_result(cells) -> tuple[dict, RadiopurityValue]:
    """ Read one row of a measurement results table from its non-empty cell
    texts. Return the row in radiopurity.org's format, and converted
    """
    extra = {}
    if cells[0] in ('=', '<'):
        # no isotope given
        cells = [''] + cells
    if len(cells) > 1 and cells[1] == '<' and len(cells) > 2 \
            and not _isfloat(cells[2]):
        # lo < isotope [< hi] unit [confidence level: N%]
        isotope, kind = cells[2], 'range'
        vals = [cells[0]]
        rest = cells[3:]
        if rest and rest[0] == '<':
            vals.append(rest[1])
            rest = rest[2:]
        unit, rest = (rest[0], rest[1:]) if rest else ('', [])
    else:
        isotope = cells[0]
        kind = 'limit' if cells[1:2] == ['<'] else 'measurement'
        vals = cells[2:3]
        unit = cells[3] if len(cells) > 3 else ''
        rest = cells[4:]
    # remaining cells are labelled extras, e.g. '±', err, unit
    for i, cell in enumerate(rest):
        if cell == '±' and i + 1 < len(rest):
            extra['err'] = rest[i+1]
        elif cell == 'asymmetric error:' and i + 1 < len(rest):
            extra['err2'] = rest[i+1]
        elif cell == 'confidence level:' and i + 1 < len(rest):
            extra['cl'] = rest[i+1].rstrip('%')
    vals += [extra[k] for k in ('err', 'err2', 'cl') if k in extra]
    result = dict(isotope=isotope, type=kind, value=vals, unit=unit)
    return result, _convert(result, extra)


def normalize_isotope(name: str) -> Optional[str]:
    """ Convert `name` to the form 'Sym-A', or None if it isn't an isotope
    """
    name = name.strip()
    if name in SHORTHAND:
        return SHORTHAND[name]
    if not (match := _isotope_name.fullmatch(name)):
        return None
    A, symbol, A2 = match.groups()
    if bool(A) == bool(A2):
        return None
    candidate = f"{symbol.capitalize()}-{int(A or A2)}"
    return candidate if get_isotope(candidate) is not None else None


def _convert_unit(unit: str) -> str:
    unit = unit.replace('μ', 'u').replace('µ', 'u')
    # e.g. cm2 -> cm**2
    return re.sub(r'([a-zA-Z])([23])\b', r'\1**\2', unit)


def _convert(result: dict, extra: dict) -> RadiopurityValue:
    isotope = result['isotope']
    name = normalize_isotope(isotope)
    warnings = []
    if name is None:
        name = isotope
        warnings.append("not a recognized isotope name")
    value = RadiopurityValue(isotope=isotope, name=name, rate=None)

    vals, unit = result['value'], _convert_unit(result['unit'])
    if not isotope:
        warnings = ["no isotope given"]
    elif result['type'] == 'range' or not vals:
        warnings = ["ranges can't be imported"]
    elif result['type'] == 'limit':
        cl = f" ({extra['cl']}%)" if 'cl' in extra else ''
        value.rate = f"< {vals[0]}{cl} {unit}"
    elif 'err2' in extra:
        value.rate = f"({vals[0]} +{extra['err']} -{extra['err2']}) {unit}"
    elif 'err' in extra:
        value.rate = f"{vals[0]} +- {extra['err']} {unit}"
    else:
        value.rate = f"{vals[0]} {unit}"

    if value.rate is not None:
        try:
            # e.g. 'mBq/unit' parses as milli-Bq per micro-nit, so also
            # insist on units that emission sources handle
            Multiplier.get_multiplier(_rate_field.to_python(value.rate))
        except Exception:
            warnings = [f"can't import units '{result['unit']}'"]
            value.rate = None
    if value.rate is not None:
        try:
            EmissionSource(name=name, rate=value.rate).clean()
        except ValidationError as e:
            warnings.append(f"not added as a source: {e.message}")
            value.source = False
    value.warning = '; '.join(warnings)
    return value


def _parse_date(dates) -> Optional[datetime.date]:
    """ Take the first of radiopurity.org's measurement dates, if it has a
    format we recognize
    """
    if not dates:
        return None
    for fmt in ('%Y-%m-%d', '%Y-%m', '%Y'):
        try:
            return datetime.datetime.strptime(dates[0].strip(), fmt).date()
        except ValueError:
            continue
    return None
