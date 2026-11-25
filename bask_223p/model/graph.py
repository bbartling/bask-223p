"""Conservative CSV -> RDF conversion for site/equipment/point topology.

Uses a *local extension vocabulary*: only human-approved, explicit class mappings
produce external rdf:type statements. Never infer 223/Brick classes from labels.
"""
from __future__ import annotations

import csv
import hashlib
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from .fqpn import parse_fqpn

VOCAB = Namespace('https://example.org/bask-223p/v2#')
REQUIRED_BACNET = ('device_instance', 'object_type', 'object_instance')


def uri_for(base: str, entity: str, name: str) -> URIRef:
    # Hash IDs avoid collisions from punctuation, whitespace, and URL normalization.
    digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:20]
    return URIRef(f'{base.rstrip("/")}/{entity}/{digest}')


def valid_external_iri(value: str) -> bool:
    parts = urlsplit(value)
    return bool(parts.scheme in ('https', 'http', 'urn') and
                ((parts.netloc and parts.scheme != 'urn') or (parts.path and parts.scheme == 'urn')))


def load_approved_mappings(path: Path | None) -> dict[str, URIRef]:
    if path is None:
        return {}
    result = {}
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not {'FQPN', 'ClassURI', 'Status'} <= set(reader.fieldnames):
            raise ValueError('Mappings CSV needs FQPN,ClassURI,Status')
        for row in reader:
            fqpn = row['FQPN'].strip()
            status = row['Status'].strip().casefold()
            iri = row['ClassURI'].strip()
            if status != 'approved':
                continue
            if not fqpn or not valid_external_iri(iri):
                raise ValueError(f'Invalid approved FQPN or ClassURI: {row!r}')
            if fqpn in result and str(result[fqpn]) != iri:
                raise ValueError(f'Conflicting mappings for {fqpn}')
            result[fqpn] = URIRef(iri)
    return result


def _read_rows(src: Path) -> list[dict]:
    with src.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or 'FQPN' not in reader.fieldnames:
            raise ValueError(f'{src} needs an FQPN column')
        rows = [dict((k, (v or '').strip()) for k, v in r.items() if k is not None)
                for r in reader if (r.get('FQPN') or '').strip()]
    if not rows:
        raise ValueError('Input CSV has no point rows')
    counts = Counter(row['FQPN'] for row in rows)
    dup = [name for name, count in counts.items() if count > 1]
    if dup:
        raise ValueError(f'Duplicate FQPN would collapse RDF nodes: {dup[:5]}')
    return rows


def build_site_graph(src: Path, base: str, mappings: Path | None = None,
                     strict_names: bool = False) -> tuple[Graph, dict]:
    if not valid_external_iri(base):
        raise ValueError('Base must be a fully qualified HTTP(S) or URN IRI')
    rows = _read_rows(src)
    approved = load_approved_mappings(mappings)
    g = Graph()
    g.bind('oc', VOCAB)
    g.bind('rdfs', RDFS)
    site_nodes, building_nodes, equipment_nodes = set(), set(), set()
    addresses: dict[tuple[str,str,str], str] = {}
    warnings: list[dict] = []
    for raw in rows:
        parsed = parse_fqpn(raw['FQPN'])
        if parsed['Problems']:
            warnings.append({'fqpn': raw['FQPN'], 'problem': parsed['Problems']})
            if strict_names:
                raise ValueError(f'{raw["FQPN"]}: {parsed["Problems"]}')
        site_name = f'{parsed["Branch"]}|{parsed["Site"]}'
        building_name = f'{site_name}|{parsed["Building"]}'
        system_name = f'{building_name}|{parsed["Domain"]}|{parsed["System"]}'
        site = uri_for(base, 'site', site_name)
        building = uri_for(base, 'building', building_name)
        equipment = uri_for(base, 'equipment', system_name)
        subsystem = (uri_for(base, 'equipment', system_name + '|' + parsed['Subsystem'])
                     if parsed['Subsystem'] else None)
        point_parent = subsystem or equipment
        point = uri_for(base, 'point', raw['FQPN'])
        site_nodes.add(site); building_nodes.add(building); equipment_nodes.add(equipment)
        if subsystem:
            equipment_nodes.add(subsystem)
            g.add((subsystem, RDF.type, VOCAB.Equipment))
            g.add((subsystem, RDFS.label, Literal(parsed['Subsystem'])))
            g.add((equipment, VOCAB.hasSubEquipment, subsystem))
            g.add((subsystem, VOCAB.parentEquipment, equipment))
            g.add((subsystem, VOCAB.subsystemName, Literal(parsed['Subsystem'])))
        g.add((site, RDF.type, VOCAB.Site)); g.add((site, RDFS.label, Literal(parsed['Site'])))
        g.add((building, RDF.type, VOCAB.Building)); g.add((building, RDFS.label, Literal(parsed['Building'])))
        g.add((site, VOCAB.hasBuilding, building))
        g.add((equipment, RDF.type, VOCAB.Equipment))
        g.add((equipment, RDFS.label, Literal(parsed['System'])))
        g.add((building, VOCAB.hasEquipment, equipment))
        g.add((point, RDF.type, VOCAB.Point))
        g.add((point, VOCAB.fqpn, Literal(raw['FQPN'])))
        g.add((point, VOCAB.pointName, Literal(parsed['PointName'])))
        g.add((point, RDFS.label, Literal(raw.get('description') or parsed['Point_meaning'] or parsed['PointName'])))
        g.add((point_parent, VOCAB.hasPoint, point))
        for col, prop in [('Domain','domain'), ('System','systemName')]:
            if parsed[col]:
                g.add((equipment, VOCAB[prop], Literal(parsed[col])))
        for col, prop in [('Site','siteName'), ('Branch','branchName'), ('Building','buildingName')]:
            subject = building if col == 'Building' else site
            if parsed[col]:
                g.add((subject, VOCAB[prop], Literal(parsed[col])))
        if parsed['Problems']:
            g.add((point, VOCAB.parseWarnings, Literal(parsed['Problems'])))
        if raw.get('bacnet_address'):
            g.add((point, VOCAB.bacnetAddressDisplay, Literal(raw['bacnet_address'])))
        if any(raw.get(k) for k in REQUIRED_BACNET):
            g.add((point, RDF.type, VOCAB.AddressedPoint))
            for key in REQUIRED_BACNET:
                val = raw.get(key, '')
                if not val:
                    continue
                if key in ('device_instance', 'object_instance'):
                    if not val.isdigit() or not (0 <= int(val) <= 4194303):
                        raise ValueError(f'{raw["FQPN"]}: invalid BACnet {key}: {val}')
                    g.add((point, VOCAB[key], Literal(int(val), datatype=XSD.integer)))
                else:
                    g.add((point, VOCAB[key], Literal(val)))
            g.add((point, VOCAB.property_identifier, Literal(raw.get('property_identifier') or 'presentValue')))
            if all(raw.get(k) for k in REQUIRED_BACNET):
                address = tuple(raw[k].strip().lower() for k in REQUIRED_BACNET)
                if address in addresses:
                    raise ValueError(f'Duplicate BACnet object {address} assigned to {raw["FQPN"]} '
                                     f'and {addresses[address]}')
                addresses[address] = raw['FQPN']
        if raw['FQPN'] in approved:
            g.add((point, RDF.type, approved[raw['FQPN']]))
    unknown_approvals = set(approved) - {r['FQPN'] for r in rows}
    if unknown_approvals:
        raise ValueError(f'Approved mapping refers to unknown point(s): {sorted(unknown_approvals)[:5]}')
    summary = {'points': len(rows), 'sites': len(site_nodes), 'buildings': len(building_nodes),
               'equipment': len(equipment_nodes), 'triples': len(g), 'warnings': warnings,
               'approved_class_assignments': len(approved),
               'model': 'local extension vocabulary, not automatic ASHRAE 223 or Brick mapping'}
    return g, summary
