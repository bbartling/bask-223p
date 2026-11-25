"""Terminology discovery only. NEVER infer owl:equivalentClass from string similarity."""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from rdflib import Graph, URIRef, Literal
from rdflib.namespace import RDF, RDFS, OWL, SKOS

try:
    from rapidfuzz import fuzz, process
except ImportError:
    fuzz = process = None


@dataclass(frozen=True)
class Candidate:
    term: str
    ontology: str
    rank: int
    status: str
    label: str
    uri: str
    score: float
    rdf_kind: str


def normalize(value: object) -> str:
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', str(value))
    return ' '.join(re.sub(r'[^\w]+', ' ', text, flags=re.UNICODE).casefold().split())


def kind_of(graph: Graph, uri: URIRef) -> str:
    if (uri, RDF.type, OWL.Class) in graph or (uri, RDF.type, RDFS.Class) in graph:
        return 'class'
    if (uri, RDF.type, RDF.Property) in graph or (uri, RDF.type, OWL.ObjectProperty) in graph or (uri, RDF.type, OWL.DatatypeProperty) in graph:
        return 'property'
    if (uri, RDF.type, SKOS.Concept) in graph:
        return 'concept'
    return 'other / tag'


def label_index(graph: Graph) -> dict[str, set[tuple[str, str]]]:
    """Index duplicate lexical labels without silently overwriting candidate URIs."""
    index: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for predicate in (SKOS.prefLabel, RDFS.label, SKOS.altLabel):
        for uri, _, label in graph.triples((None, predicate, None)):
            if isinstance(uri, URIRef) and isinstance(label, Literal):
                key = normalize(label)
                if key:
                    index[key].add((str(uri), kind_of(graph, uri)))
    return dict(index)


def find_matches(term: str, ontology: str, index: dict, top: int = 3,
                 cutoff: float = 0.86) -> list[Candidate]:
    key = normalize(term)
    if not key:
        return []
    if process is not None:
        # Explicitly use a bounded top-k, not quadratic pairwise candidate scoring.
        raw = process.extract(key, index.keys(), scorer=fuzz.ratio, limit=max(top * 4, 15), score_cutoff=cutoff * 100)
        scored = [(label, float(score) / 100) for label, score, _ in raw]
    else:
        scored = sorted(((label, SequenceMatcher(None, key, label).ratio())
                         for label in index), key=lambda item: (-item[1], item[0]))
        scored = [(label, score) for label, score in scored if score >= cutoff][:max(top*4, 15)]
    expanded = []
    for label, score in scored:
        for uri, kind in sorted(index[label]):
            expanded.append((label, uri, kind, score))
    # Sort stably, avoid hiding multiple URIs for identical labels.
    expanded.sort(key=lambda item: (-item[3], item[0], item[1]))
    return [Candidate(term=term, ontology=ontology, rank=i, label=label, uri=uri,
                      rdf_kind=kind, score=round(score, 3),
                      status='exact_label_REVIEW' if score >= 0.999 else 'fuzzy_REVIEW')
            for i, (label, uri, kind, score) in enumerate(expanded[:top], start=1)]


def load_requirements(path: Path) -> list[str]:
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or 'Requirement' not in reader.fieldnames:
            raise ValueError(f'{path} must have a Requirement column')
        return list(dict.fromkeys(r['Requirement'].strip() for r in reader if r.get('Requirement', '').strip()))


def audit(requirements: Path, catalogs: dict[str, Path], out: Path,
          cutoff: float = .86, top: int = 3) -> dict:
    if not 0 <= cutoff <= 1:
        raise ValueError('cutoff must be between 0 and 1')
    if not 1 <= top <= 20:
        raise ValueError('top must be 1..20')
    from .gap import GAP_STATUSES, GapTerm, propose_stubs, write_gap_report
    from .ingest import load_requirements as load_requirement_rows, write_requirements_csv

    reqs = load_requirement_rows(requirements)
    if not reqs:
        raise ValueError('No requirements supplied; cannot calculate coverage')
    if not catalogs:
        raise ValueError('At least one ontology catalog is required')
    indices, stats = {}, {}
    for name, source in catalogs.items():
        if not source.is_file():
            raise FileNotFoundError(f'Missing ontology: {source}')
        graph = Graph().parse(str(source), format='turtle')
        indices[name] = label_index(graph)
        stats[name] = {'triples': len(graph), 'distinct_normalized_labels': len(indices[name]), 'source': str(source)}
    # Collect below-cutoff hints so a needs-extension row can still show a closest term.
    hint_floor = min(cutoff, 0.4)
    rows: list[Candidate] = []
    unmapped = []
    gap_terms: list[GapTerm] = []
    for req in reqs:
        hints: list[Candidate] = []
        for name, index in indices.items():
            hints.extend(find_matches(req.requirement, name, index, top, hint_floor))
            if req.description and req.description.casefold() != req.requirement.casefold():
                hints.extend(find_matches(req.description, name, index, top, hint_floor))
        # Keep the best unique URI per ontology hint.
        dedup: dict[tuple[str, str], Candidate] = {}
        for hit in hints:
            key = (hit.ontology, hit.uri)
            if key not in dedup or hit.score > dedup[key].score:
                dedup[key] = hit
        hints = sorted(dedup.values(), key=lambda h: (-h.score, h.ontology, h.uri))
        strong = [h for h in hints if h.score >= cutoff][:top * max(1, len(indices))]
        rows.extend(strong)
        def _pack(hits: list[Candidate]) -> list[dict]:
            return [{'ontology': h.ontology, 'label': h.label, 'uri': h.uri,
                     'score': h.score, 'status': h.status} for h in hits]
        if not strong:
            unmapped.append(req.requirement)
            audit_status, reason = 'unmapped', 'no standard term found at the audit cutoff'
        else:
            audit_status = strong[0].status
            reason = 'lexical candidates only: check the meaning'
        gap_terms.append(GapTerm(
            row=req.row, category=req.category, requirement=req.requirement,
            description=req.description, audit_status=audit_status, reason=reason,
            reviewer_notes=req.reviewer_notes,
            candidates=_pack(strong),
            hints=_pack(hints),
        ))
    out.mkdir(parents=True, exist_ok=True)
    write_requirements_csv(reqs, out / 'requirements.csv')
    with (out / 'candidates.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(Candidate.__dataclass_fields__))
        writer.writeheader()
        writer.writerows([asdict(r) for r in rows])
    status_counts = Counter(row.status for row in rows if row.rank == 1)
    exact_terms = {r.term for r in rows if r.status == 'exact_label_REVIEW'}
    summary = {
        'version': '0.1.0', 'not_semantic_validation': True,
        'terms': len(reqs), 'terms_with_any_candidate': len(reqs) - len(unmapped),
        'terms_without_candidate': len(unmapped), 'terms_with_exact_label': len(exact_terms),
        'unmapped_terms': unmapped, 'candidate_rows': len(rows), 'top': top, 'cutoff': cutoff,
        'first_rank_counts': dict(status_counts), 'catalogs': stats,
        'interpretation': 'Candidates are lexical suggestions only. Exact spelling does not establish equivalence or coverage. Human review required.'
    }
    (out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    from .report import write_html
    write_html(rows, summary, out / 'report.html')
    write_gap_report(gap_terms, out)
    propose_stubs(gap_terms, out / 'extensions')
    from .gap import gap_status as _gs
    summary['gap_summary'] = {k: sum(1 for t in gap_terms if _gs(t) == k) for k in GAP_STATUSES}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary
