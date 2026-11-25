"""Optional Shifty workflows: inference, shape maps, failure evidence, prepared validation.

All imports are lazy so parsing and graph-building work without optional Rust wheels.
None of the operations makes live BACnet writes.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from rdflib import Graph


def _engine():
    try:
        import shifty
    except ImportError as exc:
        raise RuntimeError('Install the real Shifty engine: pip install "bask-223p[graph]"') from exc
    return shifty


def _save_json(path: Path, content: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2, sort_keys=True, default=str), encoding='utf-8')


def infer(data: Graph, shapes: Graph, out: Path, max_iterations: int = 1000) -> dict:
    """SHACL-AF to fixed point; emit original+derived graph and separate inferred delta."""
    if max_iterations < 2:
        raise ValueError('max_iterations must be >= 2 (last pass checks convergence)')
    shifty = _engine()
    result = shifty.infer(data, shapes, in_place=False,
                          on_unsupported='error', max_iterations=max_iterations)
    graph = result.graph()
    out.mkdir(parents=True, exist_ok=True)
    graph.serialize(destination=str(out/'inferred-model.ttl'), format='turtle')
    (out/'inferred-delta.nt').write_text(result.inferred_ntriples, encoding='utf-8')
    info = {
        'engine': 'shifty', 'mode': 'SHACL-AF fixed-point inference',
        'original_triples': len(data), 'result_triples': len(graph),
        'inferred_triples': result.inferred_count,
        'max_iterations': max_iterations,
        'diagnostics': [str(d) for d in result.diagnostics],
        'input_graph_modified': False,
    }
    _save_json(out/'inference-summary.json', info)
    return info


def explain(data: Graph, shapes: Graph, out: Path, *, failures_only: bool = True,
            limit: int = 300) -> dict:
    """Use Shifty EvidenceSession to report pass/fail/unselected statements."""
    if limit < 1:
        raise ValueError('limit must be positive')
    shifty = _engine()
    session = shifty.EvidenceSession(shacl_graph=shapes, data_graph=data,
                                     infer=False, graph_mode='data')
    run = session.validate(minimum_severity='violation')
    rows = []
    total_statements = 0
    unselected = []
    for statement in run.statements:
        total_statements += 1
        if not statement.selected_foci:
            unselected.append(str(statement.selector))
        for focus in statement.selected_foci:
            if failures_only and focus.status != 'fail':
                continue
            if len(rows) < limit:
                rows.append({
                    'selector': str(statement.selector),
                    'focus': str(focus.focus), 'status': str(focus.status),
                    'explanation': str(focus.evidence.explain()),
                })
    out.mkdir(parents=True, exist_ok=True)
    _save_json(out/'evidence.json', rows)
    (out/'evidence.txt').write_text('\n\n'.join(
        f"{r['status'].upper()} {r['focus']}\n{r['selector']}\n{r['explanation']}" for r in rows
    ), encoding='utf-8')
    info = {'statements': total_statements, 'unselected_targets': unselected,
            'emitted_focus_evidence': len(rows), 'limit': limit,
            'note': 'No selection is not the same as validation passing.'}
    _save_json(out/'evidence-summary.json', info)
    return info


def shape_bindings(data: Graph, shapes: Graph, out: Path, *, conforms_only: bool = False) -> dict:
    """Extract typed property bindings with Shifty shape_map; never infer point mappings."""
    shifty = _engine()
    mappings = shifty.shape_map(data, shapes, infer=False, graph_mode='data')
    records = []
    for mapping in mappings:
        shape = mapping.shape_name
        if conforms_only and not mapping.conforms:
            continue
        properties = []
        for accepted, pairs in ((True, mapping.successful), (False, mapping.unsuccessful)):
            for key, binding in pairs:
                properties.append({
                    'key': str(key), 'name': str(binding.name or ''),
                    'accepted': accepted,
                    'values': [v.n3() for v in binding.values] if binding.values is not None else None,
                })
        records.append({
            'shape': str(shape), 'focus': str(mapping.focus),
            'conforms': bool(mapping.conforms),
            'properties': properties,
            'value_map': mapping.value_map(by='name', python=True),
        })
    records.sort(key=lambda row: (row['shape'], row['focus']))
    out.mkdir(parents=True, exist_ok=True)
    _save_json(out/'shape-map.json', records)
    with (out/'bindings.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['shape', 'focus', 'conforms', 'slot', 'slot_name', 'accepted', 'value_n3'])
        for row in records:
            for prop in row['properties']:
                for value in (prop['values'] or ['']):
                    writer.writerow([row['shape'], row['focus'], row['conforms'],
                                     prop['key'], prop['name'], prop['accepted'], value])
    info = {'profiles': len({r['shape'] for r in records}), 'focus_rows': len(records),
            'conforms_only': conforms_only, 'engine': 'shifty.shape_map',
            'note': 'Bindings are model metadata. They are NOT authorization for physical BACnet writes.'}
    _save_json(out/'bindings-summary.json', info)
    return info


def batch_validate(data_files: list[Path], shapes: Graph, out: Path) -> dict:
    """Compile shapes once for repeated building models with PreparedValidator."""
    if not data_files:
        raise ValueError('At least one --data input required')
    shifty = _engine()
    validator = shifty.PreparedValidator(shapes)
    rows = []
    out.mkdir(parents=True, exist_ok=True)
    for n, source in enumerate(data_files, 1):
        graph = Graph().parse(str(source))
        conforms, report, message = validator.validate(graph, infer=False,
             minimum_severity='violation', on_unsupported='error', graph_mode='data')
        basename = f'{n:03d}'
        report.serialize(destination=str(out/f'{basename}-report.ttl'), format='turtle')
        (out/f'{basename}-report.txt').write_text(str(message), encoding='utf-8')
        rows.append({'data': str(source), 'conforms': bool(conforms),
                     'triples': len(graph), 'report': f'{basename}-report.ttl'})
    _save_json(out/'batch-summary.json', rows)
    return {'graphs': len(rows), 'passed': sum(r['conforms'] for r in rows),
            'failed': sum(not r['conforms'] for r in rows),
            'compiled_shapes_once': True,
            'diagnostics': [str(d) for d in validator.diagnostics]}


def algebra_findings(data: Graph, shapes: Graph, out: Path) -> dict:
    """Read Shifty's structured algebra reasons; useful for repair triage and FDD QA."""
    shifty = _engine()
    result = shifty.validate_algebra(data, shapes, infer=False,
        on_unsupported='error', graph_mode='data', minimum_severity='violation')
    rows = []
    for item in result.violations:
        for reason in item.reasons:
            rows.append({
                'focus_node': str(item.focus_node),
                'shape': str(item.shape_name or ''),
                'severity': str(reason.severity),
                'constraint_kind': str(reason.constraint_kind),
                'path': str(reason.path or ''),
                'value': str(reason.value or ''),
                'message': str(reason.author_message or reason.message),
                'constraint_id': str(reason.constraint_id),
            })
    rows.sort(key=lambda r:(r['focus_node'], r['constraint_kind'], r['path']))
    out.mkdir(parents=True, exist_ok=True)
    _save_json(out/'algebra-findings.json', rows)
    with (out/'algebra-findings.csv').open('w', newline='', encoding='utf-8') as f:
        fields = ['focus_node', 'shape', 'severity', 'constraint_kind', 'path',
                  'value', 'message', 'constraint_id']
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    info = {'conforms': bool(result.conforms), 'failure_reasons': len(rows),
            'violations': len(result.violations),
            'engine': 'shifty.validate_algebra',
            'diagnostics': [str(d) for d in getattr(result, 'diagnostics', ())]}
    _save_json(out/'algebra-summary.json', info)
    return info
