"""Merge edge Turtle into one dataset.

Each edge is a named graph. The default graph is the union of those graphs.
Blank nodes are skolemized with a per-edge IRI before the union, so two edges
that both used ``_:b0`` do not become one node. IRIs that already match,
including ``bacnet://<device>/<type>,<instance>``, collapse to one node.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

EDGE_TYPE = URIRef("urn:central:EdgeGraph")
EDGE_SITE = URIRef("urn:central:site")
EDGE_NAME = URIRef("urn:central:edge")


@dataclass
class Merged:
    dataset: Dataset
    union: Graph
    named: list[tuple[str, str, Graph]] = field(default_factory=list)

    @property
    def edge_triples(self) -> dict[str, int]:
        return {f"{site}/{edge}": len(graph) for site, edge, graph in self.named}

    @property
    def data_union_triples(self) -> int:
        union = Graph()
        for _site, _edge, graph in self.named:
            for triple in graph:
                union.add(triple)
        return len(union)


def _token(node: Node, sig: dict[BNode, tuple]) -> tuple:
    if isinstance(node, BNode):
        return ("b", sig.get(node, ()))
    return ("i", str(node))


def _bnode_groups(graph: Graph) -> list[list[BNode]]:
    bnodes = {node for node in graph.all_nodes() if isinstance(node, BNode)}
    if not bnodes:
        return []
    sig: dict[BNode, tuple] = {node: () for node in bnodes}
    for _ in range(8):
        nxt: dict[BNode, tuple] = {}
        for node in bnodes:
            rows = []
            for pred, obj in graph.predicate_objects(node):
                rows.append(("o", str(pred), _token(obj, sig)))
            for subj, pred in graph.subject_predicates(node):
                rows.append(("s", _token(subj, sig), str(pred)))
            nxt[node] = tuple(sorted(rows, key=repr))
        sig = nxt
    groups: dict[tuple, list[BNode]] = {}
    for node, signature in sig.items():
        groups.setdefault(signature, []).append(node)
    return [groups[key] for key in sorted(groups, key=repr)]


def skolemize(graph: Graph, site: str, edge: str) -> Graph:
    """Replace blank nodes with ``urn:central:bnode:<site>:<edge>:<n>``.

    The number comes from a neighborhood signature, not from the parser's
    blank-node label, so the same Turtle text always yields the same IRIs.
    """
    groups = _bnode_groups(graph)
    if not groups:
        return graph
    mapping: dict[BNode, URIRef] = {}
    number = 0
    for group in groups:
        for node in group:
            mapping[node] = URIRef(f"urn:central:bnode:{site}:{edge}:{number}")
            number += 1
    out = Graph()
    for prefix, ns in graph.namespaces():
        out.bind(prefix, ns)
    for subj, pred, obj in graph:
        out.add((mapping.get(subj, subj), pred, mapping.get(obj, obj)))
    return out


def graph_iri(site: str, edge: str) -> URIRef:
    return URIRef(f"urn:central:graph:{site}:{edge}")


def merge_models(parts: list[tuple[str, str, bytes]]) -> Merged:
    """``parts`` is ``(site, edge, turtle bytes)`` in config order."""
    dataset = Dataset()
    named: list[tuple[str, str, Graph]] = []
    for site, edge, payload in parts:
        parsed = Graph()
        parsed.parse(data=payload.decode("utf-8"), format="turtle")
        local = skolemize(parsed, site, edge)
        named.append((site, edge, local))
        context = dataset.graph(graph_iri(site, edge))
        for triple in local:
            context.add(triple)
        iri = graph_iri(site, edge)
        context.add((iri, RDF.type, EDGE_TYPE))
        context.add((iri, EDGE_SITE, Literal(site)))
        context.add((iri, EDGE_NAME, Literal(edge)))
    union = Graph()
    for context in dataset.graphs():
        if isinstance(context.identifier, URIRef):
            for triple in context:
                union.add(triple)
    default = dataset.default_graph
    for triple in union:
        default.add(triple)
    return Merged(dataset=dataset, union=union, named=named)


def _term(node: Node, labels: dict[BNode, str] | None = None) -> str:
    if isinstance(node, BNode):
        if not labels or node not in labels:
            raise ValueError("blank node has no stable label")
        return labels[node]
    return node.n3()


def stable_source_turtle(graph: Graph) -> str:
    """Sorted N-Triples with ``_:b0`` labels from the skolem signature.

    Parsing this text still yields blank nodes. The labels do not depend on
    the parser that built ``graph``.
    """
    labels: dict[BNode, str] = {}
    number = 0
    for group in _bnode_groups(graph):
        for node in group:
            labels[node] = f"_:b{number}"
            number += 1
    lines = [f"{_term(s, labels)} {_term(p, labels)} {_term(o, labels)} ." for s, p, o in graph]
    body = "\n".join(sorted(lines))
    return "# Sorted N-Triples (Turtle subset).\n" + (body + "\n" if body else "")


def stable_nt(graph: Graph) -> str:
    text = graph.serialize(format="nt")
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(sorted(lines))


def stable_turtle(graph: Graph) -> str:
    body = stable_nt(graph)
    return "# Sorted N-Triples (Turtle subset).\n" + (body + "\n" if body else "")


def stable_trig(merged: Merged) -> str:
    """Default graph first (the union), then one named graph per edge."""
    chunks = [
        "# Default graph: union of the named graphs, including provenance triples.",
        stable_nt(merged.union),
        "",
    ]
    for site, edge, _graph in merged.named:
        iri = graph_iri(site, edge)
        context = merged.dataset.graph(iri)
        chunks.append(f"GRAPH <{iri}> {{")
        body = stable_nt(context)
        if body:
            chunks.append(body)
        chunks.append("}")
        chunks.append("")
    return "\n".join(chunks).rstrip() + "\n"


def stable_nquads(merged: Merged) -> str:
    lines: list[str] = []
    for subj, pred, obj in merged.union:
        lines.append(f"{subj.n3()} {pred.n3()} {obj.n3()} .")
    for site, edge, _graph in merged.named:
        iri = graph_iri(site, edge)
        context = merged.dataset.graph(iri)
        for subj, pred, obj in context:
            lines.append(f"{subj.n3()} {pred.n3()} {obj.n3()} <{iri}> .")
    return "\n".join(sorted(lines)) + ("\n" if lines else "")


def jsonld_document(merged: Merged) -> dict:
    """JSON-LD named graphs. The default graph is the union and is served as Turtle."""
    import json

    graphs = []
    for site, edge, _graph in merged.named:
        iri = graph_iri(site, edge)
        context = merged.dataset.graph(iri)
        payload = json.loads(context.serialize(format="json-ld"))
        graphs.append({"@id": str(iri), "@graph": payload})
    return {"@graph": graphs}
