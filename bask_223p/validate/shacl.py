"""Shifty graph validation and OntoEnv owl:imports closure, no silent fallback."""
from __future__ import annotations

import csv
import hashlib
import inspect
import json
import os
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


# Public 223P file at https://open223.info/223p.ttl. Same bytes Open-FDD pins.
PINNED_223P_SHA256 = "08f80bd6e15052f28cdfd354bdf46ff96e5e1afdba36a4dbed5186fc1ea51c41"
S223_ROOT = "http://data.ashrae.org/standard223/1.0/model/all"
_ONTOLOGY_SUFFIXES = {".ttl", ".rdf", ".owl", ".nt", ".n3", ".jsonld"}


def _pinned_shapes_path(shapes_path: Path | None) -> Path:
    if shapes_path is None:
        raw = os.environ.get("S223_SHAPES", "").strip()
        if not raw:
            raise FileNotFoundError("Pass the pinned 223p.ttl or set S223_SHAPES")
        shapes_path = Path(raw)
    shapes_path = Path(shapes_path)
    if not shapes_path.is_file():
        raise FileNotFoundError(f"223P shapes file not found: {shapes_path}")
    digest = hashlib.sha256(shapes_path.read_bytes()).hexdigest()
    if digest != PINNED_223P_SHA256:
        raise ValueError(f"{shapes_path} sha256 {digest} does not match the pinned 223P file")
    return shapes_path


def _ontology_files(ontology_dir: Path | None) -> list[Path]:
    if ontology_dir is None:
        raw = os.environ.get("S223_ONTOLOGY_DIR", "").strip()
        ontology_dir = Path(raw) if raw else None
    if ontology_dir is None:
        return []
    if not ontology_dir.is_dir():
        raise FileNotFoundError(f"223P ontology directory not found: {ontology_dir}")
    return [
        path for path in sorted(ontology_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in _ONTOLOGY_SUFFIXES
    ]


def materialize_223p_closure(shapes_path: Path, dest: Path) -> list[str]:
    """Download the imports of the pinned 223P file into ``dest``.

    This is the only online step. ``validate_223p`` reads those files offline.
    """
    shapes_path = _pinned_shapes_path(shapes_path)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        from ontoenv import OntoEnv
    except ImportError as exc:
        raise RuntimeError('OntoEnv needed: pip install "bask-223p[graph]"') from exc
    names: list[str] = []
    with OntoEnv(temporary=True, offline=False, strict=True) as env:
        env.add(str(shapes_path), fetch_imports=True)
        missing = [str(item) for item in env.missing_imports(S223_ROOT)]
        if missing:
            raise ValueError("Unresolved owl:imports: " + ", ".join(missing))
        for name in env.list_closure(S223_ROOT):
            if str(name) == S223_ROOT:
                continue
            graph = env.get_graph(name)
            token = hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:16]
            graph.serialize(destination=str(dest / f"{token}.ttl"), format="turtle")
            names.append(str(name))
    return names


def _result_rows(report: Graph) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    violations: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for result in report.subjects(RDF.type, SH.ValidationResult):
        severity = str(report.value(result, SH.resultSeverity) or "")
        row = {
            "severity": severity.rsplit("#", 1)[-1],
            "focus": str(report.value(result, SH.focusNode) or ""),
            "path": str(report.value(result, SH.resultPath) or ""),
            "message": str(report.value(result, SH.resultMessage) or ""),
            "shape": str(report.value(result, SH.sourceShape) or ""),
        }
        if severity.endswith("Warning"):
            warnings.append(row)
        elif severity.endswith("Info"):
            continue
        else:
            violations.append(row)
    return violations, warnings


def validate_223p(
    model_path: Path,
    *,
    shapes_path: Path | None = None,
    ontology_dir: Path | None = None,
    cross_check: str | None = None,
) -> dict:
    """OntoEnv 0.6.4 closure plus Shifty 0.6.0 defaults.

    ``conforms`` is Shifty's result at ``minimum_severity="Violation"``.
    A pyshacl cross-check is recorded only when requested and never changes it.
    """
    model_path = Path(model_path)
    if not model_path.is_file():
        raise FileNotFoundError(model_path)
    shapes_path = _pinned_shapes_path(shapes_path)
    if cross_check not in {None, "pyshacl"}:
        raise ValueError("cross_check must be 'pyshacl' or omitted")
    try:
        from ontoenv import OntoEnv
    except ImportError as exc:
        raise RuntimeError('OntoEnv needed: pip install "bask-223p[graph]"') from exc
    try:
        import shifty
    except ImportError as exc:
        raise RuntimeError('Shifty needed: pip install "bask-223p[graph]"') from exc

    files = [shapes_path, *_ontology_files(ontology_dir)]
    with OntoEnv(temporary=True, offline=True, strict=True) as env:
        for path in files:
            _add_local_ontology(env, str(path))
        missing = [str(item) for item in env.missing_imports(S223_ROOT)]
        if missing:
            raise ValueError("Unresolved owl:imports: " + ", ".join(missing))
        closure, names = env.copy_closure(S223_ROOT)
    if not len(closure):
        raise ValueError(f"Empty dependency closure for {S223_ROOT}")

    data = load_graph(model_path)
    conforms, report, text = shifty.validate(data, closure, minimum_severity="Violation")
    violations, warnings = _result_rows(report)
    info = {
        "conforms": bool(conforms),
        "engine": "shifty",
        "shapes": str(shapes_path),
        "closure": [str(name) for name in names],
        "violations": violations,
        "warnings": warnings,
        "violation_count": len(violations),
        "warning_count": len(warnings),
        "report_text": str(text)[:20000],
        "minimum_severity": "Violation",
    }
    if cross_check == "pyshacl":
        from pyshacl import validate as pyshacl_validate

        cross_conforms, _cross_report, cross_text = pyshacl_validate(
            data_graph=data,
            shacl_graph=closure,
            advanced=True,
            inference="none",
            abort_on_first=False,
        )
        info["cross_check"] = {
            "engine": "pyshacl",
            "advanced": True,
            "conforms": bool(cross_conforms),
            "report_text": str(cross_text)[:4000],
        }
    return info
