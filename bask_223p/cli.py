"""Command line entry points. All generated RDF is local unless --online explicitly used."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from rdflib import Graph
from rdflib.namespace import OWL, RDF, SH

from .audit.coverage import audit
from .model.fqpn import parse_csv
from .model.graph import build_site_graph
from .validate.advanced import algebra_findings, batch_validate, explain, infer, shape_bindings
from .validate.shacl import closure_from_directory, load_graph, shacl_validate


def parse_catalogs(values: list[str]) -> dict[str, Path]:
    sources = {}
    for item in values:
        if '=' not in item:
            raise ValueError(f'--catalog expects Name=path: {item!r}')
        name, path = item.split('=', 1)
        if not name or not path or name in sources:
            raise ValueError(f'Invalid or duplicate --catalog entry: {item!r}')
        sources[name] = Path(path)
    return sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='bask-223p',
        description='baskStream (Niagara) -> ASHRAE 223P + Brick TTL maker, BACnet scan reconcile, '
                    'plus secondary coverage and SHACL tools')
    sub = parser.add_subparsers(dest='command', required=True)
    # Primary path: baskStream (Niagara) -> ASHRAE 223P TTL, then reconcile with a BACnet scan.
    e2 = sub.add_parser('export-223p', help='read-only baskStream or inventory -> external ASHRAE 223P + Brick TTL')
    e2.add_argument('--snapshot', type=Path, help='recorded baskStream discovery snapshot JSON')
    e2.add_argument('--input', type=Path, help='BACnet CSV or scan TTL fallback')
    e2.add_argument('--station', help='live baskStream station base URL, e.g. https://localhost')
    e2.add_argument('--slot-base', default='slot:/Drivers', help='Niagara ORD root for live discovery')
    e2.add_argument('--env-file', type=Path, help='env file with NIAGARA_USER and NIAGARA_PASS (never printed)')
    e2.add_argument('--insecure', action='store_true', help='skip TLS certificate verification (localhost self-signed)')
    e2.add_argument('--base', required=True, help='namespace base IRI for equipment, point, and ref nodes')
    e2.add_argument('--mappings', type=Path, help='optional reviewed ClassURI CSV (never auto-guessed)')
    e2.add_argument('--catalog', action='append', default=[], help='optional Name=ontology.ttl for candidate REVIEW check')
    e2.add_argument('--manifest', type=Path, help='also write CSV/JSON BACnet binding manifest')
    e2.add_argument('--validate-out', type=Path, help='directory for validation-223p.json and coverage-223p.json')
    e2.add_argument('--infer-demo-ids', action='store_true', help='assign deterministic demo BACnet IDs if simulator omits proxyExt')
    e2.add_argument('--ws-auth-header', action='store_true',
                    help='send an Authorization header on the WebSocket handshake (service requireAuthorizationHeader=true)')
    e2.add_argument('--limit', type=int, default=1000, help='max nodes per baskStream search during live discovery')
    e2.add_argument('--save-snapshot', type=Path, help='write the credential-free live discovery snapshot JSON for offline reruns')
    e2.add_argument('--shapes', type=Path,
                    help='official 223P SHACL shapes (223p.ttl); run SHACL via Shifty or pyshacl')
    e2.add_argument('--out', type=Path, default=Path('outputs/site-223p.ttl'))
    rc = sub.add_parser('reconcile', aliases=['match'],
                        help='join a Niagara export with a direct BACnet scan by device + object identifier')
    rc.add_argument('--niagara', type=Path, required=True, help='export-223p TTL or baskStream discovery snapshot JSON')
    rc.add_argument('--scan', type=Path, required=True, help='BACpypes3 site-scan TTL/CSV or any BACnet CSV/TTL')
    rc.add_argument('--out', type=Path, default=Path('outputs/reconcile'), help='directory for reconcile.json/.csv')
    rc.add_argument('--merged-out', type=Path, help='also write Niagara + scan refs as one TTL and validate it')
    rc.add_argument('--base', help='namespace base for scan-only refs (defaults to the Niagara TTL base)')
    rc.add_argument('--all-object-types', action='store_true',
                    help='also report non-point objects (file, program, schedule, ...) as missing')
    rc.add_argument('--strict', action='store_true', help='exit 1 when anything is missing on either side')
    # Secondary: inventory refs, Haystack check, terminology coverage, site models, SHACL.
    a = sub.add_parser('audit', help='lexical candidates only; not semantic graph validation')
    a.add_argument('--requirements', type=Path, required=True)
    a.add_argument('--catalog', action='append', default=[], help='repeat Name=ontology.ttl')
    a.add_argument('--out', type=Path, default=Path('outputs/audit'))
    a.add_argument('--cutoff', type=float, default=.86)
    a.add_argument('--top', type=int, default=3)
    ing = sub.add_parser('ingest', help='convert a loose workbook or inventory to a requirements CSV')
    ing.add_argument('--workbook', type=Path, required=True)
    ing.add_argument('--out', type=Path, default=Path('outputs/requirements.csv'))
    f = sub.add_parser('parse', help='parse draft FQPN point naming CSV')
    f.add_argument('--csv', type=Path, required=True)
    f.add_argument('--out', type=Path, default=Path('outputs/parsed.csv'))
    b = sub.add_parser('build', help='convert site point CSV to graph with explicit topology')
    b.add_argument('--csv', type=Path, required=True)
    b.add_argument('--base', required=True, help='namespace base IRI')
    b.add_argument('--mappings', type=Path, help='reviewed ClassURI assignments only')
    b.add_argument('--strict-names', action='store_true')
    b.add_argument('--out', type=Path, default=Path('outputs/site.ttl'))
    v = sub.add_parser('validate', help='validate RDF data using Shifty SHACL')
    v.add_argument('--data', type=Path, required=True)
    v.add_argument('--shapes', action='append', type=Path, default=[])
    v.add_argument('--imports-dir', type=Path)
    v.add_argument('--root-iri', help='ontology IRI to obtain OntoEnv closure')
    v.add_argument('--online', action='store_true', help='explicitly allow remote imports (default OFF)')
    v.add_argument('--out', type=Path, default=Path('outputs/validation'))
    v.add_argument('--min-severity', choices=['info','warning','violation'], default='violation')
    c = sub.add_parser('closure', help='materialize OntoEnv imports closure for local ontologies')
    c.add_argument('--imports-dir', type=Path, required=True)
    c.add_argument('--root-iri', required=True)
    c.add_argument('--online', action='store_true')
    c.add_argument('--out', type=Path, default=Path('outputs/closure.ttl'))
    i = sub.add_parser('inspect', help='basic graph structure diagnostics')
    i.add_argument('--data', type=Path, required=True)
    i.add_argument('--out', type=Path, default=Path('outputs/graph-inspection.json'))
    alg = sub.add_parser('findings', help='Shifty validate_algebra structured reasons for automation')
    alg.add_argument('--data', type=Path, required=True)
    alg.add_argument('--shapes', type=Path, required=True)
    alg.add_argument('--out', type=Path, default=Path('outputs/findings'))
    inf = sub.add_parser('infer', help='explicit SHACL-AF rules: write merged model plus inferred delta')
    inf.add_argument('--data', type=Path, required=True)
    inf.add_argument('--shapes', type=Path, required=True)
    inf.add_argument('--out', type=Path, default=Path('outputs/inference'))
    inf.add_argument('--max-iterations', type=int, default=1000)
    ev = sub.add_parser('explain', help='Shifty EvidenceSession failure/pass evidence and unselected targets')
    ev.add_argument('--data', type=Path, required=True)
    ev.add_argument('--shapes', type=Path, required=True)
    ev.add_argument('--out', type=Path, default=Path('outputs/evidence'))
    ev.add_argument('--include-pass', action='store_true')
    ev.add_argument('--limit', type=int, default=300)
    sm = sub.add_parser('bindings', help='Shifty shape_map: extract equipment-point application bindings')
    sm.add_argument('--data', type=Path, required=True)
    sm.add_argument('--shapes', type=Path, required=True)
    sm.add_argument('--out', type=Path, default=Path('outputs/bindings'))
    sm.add_argument('--conforms-only', action='store_true')
    bat = sub.add_parser('batch', help='PreparedValidator: compile shapes once, validate many models')
    bat.add_argument('--data', action='append', type=Path, required=True)
    bat.add_argument('--shapes', type=Path, required=True)
    bat.add_argument('--out', type=Path, default=Path('outputs/batch'))
    q = sub.add_parser('query', help='run local SPARQL query against the RDF site graph')
    ex = sub.add_parser('export-bacnet-refs', help='export BACnet device/object ids as Brick and 223P references')
    ex.add_argument('--input', type=Path, required=True, help='point CSV or a BACpypes3-style site-scan TTL')
    ex.add_argument('--base', required=True, help='namespace base IRI for point/reference nodes')
    ex.add_argument('--out', type=Path, default=Path('outputs/bacnet-refs.ttl'))
    ex.add_argument('--manifest', type=Path, help='also write a CSV/JSON point-binding table')
    eb = sub.add_parser('export-bindings', help='rebuild a point-binding CSV/JSON from a reference Turtle graph')
    eb.add_argument('--data', type=Path, required=True)
    eb.add_argument('--out', type=Path, default=Path('outputs/point-bindings.csv'))
    ch = sub.add_parser('check-haystack', help='read-only nHaystack/grid check: present, tagged, bound')
    ch.add_argument('--expected', type=Path, required=True, help='binding CSV/JSON or reference TTL')
    ch.add_argument('--grid', type=Path, help='exported Haystack grid fixture (JSON, Zinc, or CSV)')
    ch.add_argument('--url', help='live nHaystack base URL, e.g. https://localhost/haystack')
    ch.add_argument('--env-file', type=Path,
                    help='env file with NIAGARA_USER and NIAGARA_PASS (values are never printed)')
    ch.add_argument('--insecure', action='store_true',
                    help='skip TLS certificate verification (for a localhost self-signed station cert)')
    ch.add_argument('--out', type=Path, default=Path('outputs/haystack-check'))
    q.add_argument('--data', type=Path, required=True)
    q.add_argument('--sparql', type=Path, required=True)
    q.add_argument('--out', type=Path, default=Path('outputs/query-results.csv'))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'audit':
            results = audit(args.requirements, parse_catalogs(args.catalog), args.out, args.cutoff, args.top)
        elif args.command == 'ingest':
            from .audit.ingest import load_requirements, write_requirements_csv
            dest = args.out if args.out.suffix.lower() == '.csv' else args.out / 'requirements.csv'
            rows = load_requirements(args.workbook)
            write_requirements_csv(rows, dest)
            results = {'ingested': len(rows), 'output': str(dest)}
        elif args.command == 'parse':
            results = parse_csv(args.csv, args.out)
        elif args.command == 'build':
            graph, results = build_site_graph(args.csv, args.base, args.mappings, args.strict_names)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            graph.serialize(destination=str(args.out), format='turtle')
            args.out.with_suffix('.summary.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
            results['output'] = str(args.out)
        elif args.command == 'closure':
            graph, names = closure_from_directory(args.root_iri, args.imports_dir, args.online)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            graph.serialize(destination=str(args.out), format='turtle')
            results = {'triples': len(graph), 'ontologies': names, 'output': str(args.out)}
        elif args.command == 'validate':
            if bool(args.imports_dir) != bool(args.root_iri):
                raise ValueError('--imports-dir and --root-iri must be provided together')
            if args.online and not args.imports_dir:
                raise ValueError('--online only applies to OntoEnv import resolution')
            data, shapes = load_graph(args.data), Graph()
            for shape_file in args.shapes:
                shapes += load_graph(shape_file)
            deps = []
            if args.imports_dir:
                closure, deps = closure_from_directory(args.root_iri, args.imports_dir, args.online)
                shapes += closure
            if not args.shapes and not deps:
                raise ValueError('Supply --shapes or a local --imports-dir/--root-iri pair')
            results = shacl_validate(data, shapes, args.out, args.min_severity)
            results['imports_closure'] = deps
            print(json.dumps(results, indent=2))
            return 0 if results['conforms'] else 1
        elif args.command == 'findings':
            results = algebra_findings(load_graph(args.data), load_graph(args.shapes), args.out)
            print(json.dumps(results, indent=2))
            return 0 if results['conforms'] else 1
        elif args.command == 'infer':
            results = infer(load_graph(args.data), load_graph(args.shapes), args.out, args.max_iterations)
        elif args.command == 'explain':
            results = explain(load_graph(args.data), load_graph(args.shapes), args.out,
                              failures_only=not args.include_pass, limit=args.limit)
        elif args.command == 'bindings':
            results = shape_bindings(load_graph(args.data), load_graph(args.shapes), args.out,
                                     conforms_only=args.conforms_only)
        elif args.command == 'batch':
            results = batch_validate(args.data, load_graph(args.shapes), args.out)
            print(json.dumps(results, indent=2))
            return 0 if results['failed'] == 0 else 1
        elif args.command == 'inspect':
            graph = load_graph(args.data)
            preds = Counter(str(p) for _, p, _ in graph)
            types = Counter(str(o) for _, _, o in graph.triples((None, RDF.type, None)))
            results = {'triples': len(graph), 'unique_subjects': len(set(graph.subjects())),
                       'predicates_top_20': preds.most_common(20), 'types_top_20': types.most_common(20),
                       'owl_imports': sorted({str(obj) for obj in graph.objects(None, OWL.imports)}),
                       'shacl_node_shapes': len(set(graph.subjects(RDF.type, SH.NodeShape)))}
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(results, indent=2), encoding='utf-8')
        elif args.command == 'export-bacnet-refs':
            from .bacnet.refs import export_bacnet_refs
            results = export_bacnet_refs(args.input, args.base, args.out, args.manifest)
        elif args.command == 'export-223p':
            from .bacnet.export223 import export_223p
            cats = parse_catalogs(args.catalog) if args.catalog else None
            results = export_223p(
                base=args.base,
                out=args.out,
                snapshot_path=args.snapshot,
                input_path=args.input,
                station=args.station,
                slot_base=args.slot_base,
                env_file=args.env_file,
                insecure=args.insecure,
                mappings_path=args.mappings,
                catalogs=cats,
                manifest=args.manifest,
                validate_out=args.validate_out,
                infer_demo_ids=args.infer_demo_ids,
                ws_auth_header=args.ws_auth_header,
                discovery_limit=args.limit,
                save_snapshot=args.save_snapshot,
                official_shapes=args.shapes,
            )
            print(json.dumps(results, indent=2))
            ok = results.get('validation', {}).get('conforms', True)
            official = results.get('official_223p') or {}
            if official and not official.get('conforms', True):
                ok = False
            return 0 if ok else 1
        elif args.command in ('reconcile', 'match'):
            from .bacnet.reconcile import run_reconcile
            results = run_reconcile(args.niagara, args.scan, args.out, base=args.base,
                                    merged_out=args.merged_out, all_object_types=args.all_object_types)
            print(json.dumps(results, indent=2))
            if results.get('merged') and not results['merged']['validation']['conforms']:
                return 1
            if args.strict and (results['missing_in_niagara'] or results['missing_in_scan']):
                return 1
            return 0
        elif args.command == 'export-bindings':
            from .bacnet.refs import export_bindings
            results = export_bindings(args.data, args.out)
        elif args.command == 'check-haystack':
            from .bacnet.haystack import run_check
            if bool(args.grid) == bool(args.url):
                raise ValueError('Provide exactly one of --grid or --url')
            results = run_check(args.expected, args.out, grid_path=args.grid, url=args.url,
                                env_file=args.env_file, insecure=args.insecure)
            print(json.dumps(results, indent=2))
            if results.get('login_required') or results.get('reachable') is False:
                return 2
            return 0 if results.get('passed') else 1
        elif args.command == 'query':
            import csv
            graph = load_graph(args.data)
            query = args.sparql.read_text(encoding='utf-8')
            result = graph.query(query)
            if result.type != 'SELECT':
                raise ValueError('Only SELECT SPARQL queries are supported for CSV export')
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open('w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([str(v) for v in result.vars])
                writer.writerows([str(t) if t is not None else '' for t in row] for row in result)
            results = {'output': str(args.out), 'columns': [str(v) for v in result.vars]}
        else:
            raise ValueError(f'Unsupported command: {args.command}')
        print(json.dumps(results, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
