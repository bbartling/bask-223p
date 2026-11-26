"""RDF knowledge base backed by pyoxigraph for fast SPARQL + OntoEnv/Shifty validation."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pyoxigraph
from rdflib import Graph

from .sparql_guard import validate_read_only_sparql


class RdfKnowledgeBase:
    """Pairs rdflib Turtle with pyoxigraph SPARQL. 223P validation uses the shared baseline."""

    def __init__(
        self,
        graph: Graph | None = None,
        *,
        sidecar_graphs: dict[str, Graph] | None = None,
    ) -> None:
        self.graph: Graph = graph if graph is not None else Graph()
        self.sidecar_graphs: dict[str, Graph] = {
            k: v for k, v in (sidecar_graphs or {}).items() if v is not None and len(v) > 0
        }
        self.store: pyoxigraph.Store = pyoxigraph.Store()
        if len(self.graph) > 0 or self.sidecar_graphs:
            self._sync_oxigraph()

    def _sync_oxigraph(self) -> None:
        self.store = pyoxigraph.Store()
        ttl_bytes = self.graph.serialize(format="turtle").encode("utf-8")
        if ttl_bytes.strip():
            self.store.load(ttl_bytes, format=pyoxigraph.RdfFormat.TURTLE)
        for named_iri, sidecar in self.sidecar_graphs.items():
            s_bytes = sidecar.serialize(format="turtle").encode("utf-8")
            if s_bytes.strip():
                self.store.load(
                    s_bytes,
                    format=pyoxigraph.RdfFormat.TURTLE,
                    to_graph=pyoxigraph.NamedNode(named_iri),
                )

    @property
    def triple_count(self) -> int:
        return len(self.store)

    def resolve_imports(self, ontology_dirs: list[Path] | None = None, *, offline: bool = True) -> dict[str, Any]:
        """Use OntoEnv to resolve owl:imports closures into the query graph."""
        from ontoenv import OntoEnv

        search_dirs = [str(p) for p in (ontology_dirs or []) if p.exists()]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_ttl = Path(tmpdir) / "model.ttl"
            tmp_ttl.write_text(self.graph.serialize(format="turtle"), encoding="utf-8")
            env = OntoEnv(
                temporary=True,
                search_directories=search_dirs,
                offline=offline,
                strict=False,
            )
            try:
                for d in ontology_dirs or []:
                    if d.is_dir():
                        for ont in sorted(d.rglob("*")):
                            if ont.is_file() and ont.suffix.lower() in {".ttl", ".rdf", ".owl", ".nt"}:
                                env.add_no_imports(str(ont)) if False else env.add(str(ont))
                name = env.add(str(tmp_ttl))
                closure_res = env.copy_closure(name)
                closure = closure_res[0] if isinstance(closure_res, tuple) else closure_res
                if len(closure) > 0:
                    self.graph = closure
                    self._sync_oxigraph()
            finally:
                env.close()
        return {"engine": "ontoenv", "triples": len(self.graph)}

    def validate(self, shapes_path: Path | None = None) -> dict[str, Any]:
        """Validate with ``validate_223p``. A missing Shifty or pin is an error."""
        if len(self.graph) == 0:
            return {"conforms": True, "engine": "none", "triples": 0, "report": "empty graph"}
        from bask_223p.validate.shacl import validate_223p

        with tempfile.NamedTemporaryFile(suffix=".ttl", delete=False) as handle:
            model = Path(handle.name)
        try:
            model.write_text(self.graph.serialize(format="turtle"), encoding="utf-8")
            report = validate_223p(model, shapes_path=shapes_path)
        finally:
            model.unlink(missing_ok=True)
        report["triples"] = len(self.graph)
        return report

    def query(self, sparql: str, *, max_rows: int = 1000) -> dict[str, Any]:
        clean = validate_read_only_sparql(sparql)
        res = self.store.query(clean)
        if isinstance(res, (bool, pyoxigraph.QueryBoolean)):
            return {"type": "ASK", "engine": "pyoxigraph", "boolean": bool(res)}
        if isinstance(res, pyoxigraph.QueryTriples):
            out_g = Graph()
            ttl_lines = []
            for q in res:
                ttl_lines.append(f"{q.subject} {q.predicate} {q.object} .")
            if ttl_lines:
                out_g.parse(data="\n".join(ttl_lines), format="nt")
            return {
                "type": "CONSTRUCT",
                "engine": "pyoxigraph",
                "turtle": out_g.serialize(format="turtle"),
            }
        cols = [v.value for v in res.variables]
        rows: list[dict[str, str | None]] = []
        truncated = False
        for i, sol in enumerate(res):
            if i >= max_rows:
                truncated = True
                break
            row: dict[str, str | None] = {}
            for col in cols:
                term = sol[col]
                row[col] = term.value if term is not None else None
            rows.append(row)
        return {
            "type": "SELECT",
            "engine": "pyoxigraph",
            "columns": cols,
            "count": len(rows),
            "truncated": truncated,
            "rows": rows,
        }

    def serialize_turtle(self) -> str:
        return self.graph.serialize(format="turtle")
