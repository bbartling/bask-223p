"""Shifty graph validation and OntoEnv owl:imports closure, no silent fallback."""
from __future__ import annotations

import csv
import inspect
import json
from pathlib import Path

from rdflib import Graph
from rdflib.namespace import RDF, SH


def load_graph(path: Path) -> Graph:
    if not path.is_file():
        raise FileNotFoundError(path)
    return Graph().parse(str(path))


def _accepts_kwarg(fn, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _add_local_ontology(env, location: str) -> None:
    """Register a local ontology without fetching remote owl:imports.

    OntoEnv 0.6.4 still accepts ``add(..., fetch_imports=False)``. Older
    builds may only have ``add_no_imports`` or a bare ``add``.
    """
    add = getattr(env, "add", None)
    if callable(add) and _accepts_kwarg(add, "fetch_imports"):
        add(location, fetch_imports=False)
        return
    no_imports = getattr(env, "add_no_imports", None)
    if callable(no_imports):
        no_imports(location)
        return
    if add is None:
        raise RuntimeError("OntoEnv has no add() method")
    add(location)


def closure_from_directory(root_iri: str, directory: Path, online: bool = False) -> tuple[Graph, list[str]]:
    """Load local ontology files, resolve owl:imports with OntoEnv, fail if missing.

    Remote fetching is OFF by default. Explicit --online enables fetching missing
    dependencies; do not use it on untrusted ontologies without reviewing imports.

    OntoEnv 0.6.4 ``search_directories=`` plus ``offline=True`` and ``strict=True``
    tries to HTTP-fetch ``owl:imports`` IRIs during construction and raises before
    local files can satisfy them. Every ontology in ``directory`` is therefore
    added explicitly with imports-fetch disabled, then the already-loaded graphs
    are used to resolve the closure.
    """
    try:
        from ontoenv import OntoEnv
    except ImportError as exc:
        raise RuntimeError('OntoEnv needed: pip install "bask-223p[graph]"') from exc
    sources = [p for p in sorted(directory.rglob('*')) if p.is_file() and p.suffix.lower() in
               {'.ttl', '.rdf', '.owl', '.n3', '.nt', '.jsonld'}]
    if not sources:
        raise ValueError(f'No ontology files found in {directory}')
    # Do not pass search_directories here: 0.6.4 raises under offline+strict
    # when a local owl:imports IRI is not yet mapped. Adding each file is enough.
    with OntoEnv(temporary=True, offline=not online, strict=True) as env:
        for source in sources:
            _add_local_ontology(env, str(source))
        # Explicit online opt-in, avoids unexpected fetches from uncontrolled owl:imports.
        if online:
            env.update()
        missing = list(env.missing_imports(root_iri))
        if missing:
            raise ValueError('Unresolved owl:imports: ' + ', '.join(map(str, missing)))
        closure, names = env.copy_closure(root_iri)
    if not len(closure):
        raise ValueError(f'Empty dependency closure for {root_iri}')
    return closure, list(map(str, names))


def shacl_validate(data: Graph, shapes: Graph, output: Path,
                   minimum_severity: str = 'violation') -> dict:
    if not any(shapes.triples((None, RDF.type, SH.NodeShape))) and not any(
            shapes.triples((None, RDF.type, SH.PropertyShape))):
        raise ValueError('Shapes graph contains no SHACL NodeShape or PropertyShape')
    try:
        import shifty
    except ImportError as exc:
        raise RuntimeError('Shifty needed: pip install "bask-223p[graph]"') from exc
    # Keep inference explicit and off: inference changes the facts one validates.
    conforms, report, results_text = shifty.validate(
        data, shapes, minimum_severity=minimum_severity, infer=False,
        on_unsupported='error', graph_mode='data')
    output.mkdir(parents=True, exist_ok=True)
    report.serialize(destination=str(output / 'validation-report.ttl'), format='turtle')
    rows = []
    for result in report.subjects(RDF.type, SH.ValidationResult):
        rows.append({key: str(report.value(result, pred) or '')
                     for key, pred in [('focus_node', SH.focusNode), ('path', SH.resultPath),
                                       ('severity', SH.resultSeverity), ('message', SH.resultMessage),
                                       ('source_shape', SH.sourceShape),
                                       ('component', SH.sourceConstraintComponent)]})
    rows.sort(key=lambda r: (r['focus_node'], r['path'], r['message']))
    with (output / 'violations.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['focus_node', 'path', 'severity', 'message', 'source_shape', 'component'])
        writer.writeheader(); writer.writerows(rows)
    (output / 'validation.txt').write_text(results_text, encoding='utf-8')
    info = {'conforms': bool(conforms), 'results': len(rows), 'input_triples': len(data),
            'shapes_triples': len(shapes), 'minimum_severity': minimum_severity,
            'inference': False, 'engine': 'shifty'}
    (output / 'validation-summary.json').write_text(json.dumps(info, indent=2), encoding='utf-8')
    return info
