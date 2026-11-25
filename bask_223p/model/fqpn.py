"""Lossless parsing of a *draft* fully-qualified point name (FQPN) convention; no speculative RDF typing."""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

DOMAINS = frozenset({'HVAC', 'ELEC', 'ELECTRICAL', 'ESS', 'FP', 'METER', 'UTIL'})
BRANCHES = frozenset({'LAB', 'DEMO', 'SITE', 'CAMPUS', 'ORG'})
TOKENS = {
    'SA': 'Supply Air', 'DA': 'Discharge Air', 'RA': 'Return Air',
    'OA': 'Outside Air', 'MA': 'Mixed Air', 'EA': 'Exhaust Air',
    'ZN': 'Zone', 'CHW': 'Chilled Water', 'HW': 'Hot Water',
    'CW': 'Condenser Water', 'DMPR': 'Damper', 'VLV': 'Valve',
    'FAN': 'Fan', 'T': 'Temperature', 'P': 'Pressure',
    'H': 'Humidity', 'F': 'Flow', 'SP': 'Setpoint',
    'S': 'Status', 'C': 'Command', 'SPD': 'Speed', 'POS': 'Position',
}
FIELDS = ['FQPN', 'Branch', 'Site', 'Subsite', 'Building', 'Domain',
          'System', 'Subsystem', 'PointName', 'Point_meaning', 'Problems']


def is_building(value: str) -> bool:
    return value.isdigit() or (value.upper().startswith('B') and value[1:].isdigit())


def parse_fqpn(value: str) -> dict[str, str]:
    raw = str(value).strip()
    row = dict.fromkeys(FIELDS, '')
    row['FQPN'] = raw
    parts = raw.split('_') if raw else []
    issues: list[str] = []
    if len(parts) < 5:
        issues.append(f'expected >=5 fields; found {len(parts)}')
    if any(not field for field in parts):
        issues.append('empty segment')
    if parts:
        row['Branch'] = parts[0]
        if parts[0].upper() not in BRANCHES:
            issues.append('unrecognized org')
    if len(parts) > 1:
        row['Site'] = parts[1]
    if len(parts) >= 3:
        row['PointName'] = parts[-1]
    mid = parts[2:-1] if len(parts) >= 4 else []
    domain_indices = [i for i, part in enumerate(mid) if part.upper() in DOMAINS]
    if len(domain_indices) != 1:
        issues.append(f'expected exactly one domain; found {len(domain_indices)}')
    if domain_indices:
        i = domain_indices[0]
        row['Domain'] = mid[i]
        before, after = mid[:i], mid[i+1:]
    else:
        before, after = mid, []
    buildings = [part for part in mid if is_building(part)]
    if len(buildings) != 1:
        issues.append(f'expected exactly one building; found {len(buildings)}')
    if buildings:
        row['Building'] = buildings[0]
    leftovers = [part for part in before if part != row['Building']]
    row['Subsite'] = '_'.join(leftovers)
    after = [part for part in after if part != row['Building']]
    if after:
        row['System'] = after[0]
        row['Subsystem'] = '_'.join(after[1:])
    else:
        issues.append('missing system after domain')
    if not row['Site']:
        issues.append('missing site')
    tokens = row['PointName'].upper().split('-') if row['PointName'] else []
    unknown = [token for token in tokens if token not in TOKENS]
    row['Point_meaning'] = ' '.join(TOKENS[t] for t in tokens if t in TOKENS)
    if unknown:
        issues.append('unknown point tokens: ' + ', '.join(unknown))
    if not row['PointName']:
        issues.append('missing point name')
    row['Problems'] = '; '.join(dict.fromkeys(issues))
    return row


def parse_csv(src: Path, dst: Path) -> dict:
    with src.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or 'FQPN' not in reader.fieldnames:
            raise ValueError(f'{src} must have an FQPN column')
        names = [row['FQPN'].strip() for row in reader if row.get('FQPN', '').strip()]
    seen = Counter(names)
    rows = [parse_fqpn(name) for name in names]
    for row in rows:
        if seen[row['FQPN']] > 1:
            row['Problems'] = '; '.join(filter(None, [row['Problems'], 'duplicate FQPN']))
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return {'rows': len(rows), 'flagged': sum(bool(r['Problems']) for r in rows), 'output': str(dst)}
