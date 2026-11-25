"""Optional pyoxigraph SPARQL over the loaded 223P graph."""
from __future__ import annotations

from typing import Any

from rdflib import Graph


def validate_read_only_sparql(query: str) -> str:
    import re

    text = (query or "").strip()
    if not text:
        raise ValueError("SPARQL query is empty.")
    if re.search(r"(?i)(?:^|[\s;])(INSERT|DELETE|DROP|CLEAR|LOAD|CREATE|COPY|MOVE|ADD)\b", text):
        raise ValueError("Only read-only SPARQL queries are permitted.")
    return text


class SparqlKb:
    def __init__(self, graph: Graph | None) -> None:
        self.graph = graph or Graph()
        self.store: Any = None
        self.enabled = False
        if len(self.graph) == 0:
            return
        try:
            import pyoxigraph
        except ImportError:
            return
        self.store = pyoxigraph.Store()
        ttl = self.graph.serialize(format="turtle").encode("utf-8")
        if ttl.strip():
            self.store.load(ttl, format=pyoxigraph.RdfFormat.TURTLE)
            self.enabled = True

    @property
    def triple_count(self) -> int:
        if not self.enabled or self.store is None:
            return len(self.graph)
        return len(self.store)

    def query(self, query_text: str) -> dict[str, Any]:
        if not self.enabled or self.store is None:
            raise RuntimeError("SPARQL disabled (set EDGE_ENABLE_SPARQL=1 and install pyoxigraph)")
        clean = validate_read_only_sparql(query_text)
        results = self.store.query(clean)
        if results.variables is not None:
            cols = [str(v) for v in results.variables]
            rows = [
                {cols[i]: str(val) if val is not None else None for i, val in enumerate(row)}
                for row in results
            ]
            return {"type": "SELECT", "engine": "pyoxigraph", "columns": cols, "count": len(rows), "rows": rows}
        return {"type": "NON_SELECT", "engine": "pyoxigraph", "columns": [], "count": 0, "rows": []}