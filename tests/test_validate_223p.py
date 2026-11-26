"""The 223P baseline is OntoEnv's offline closure and Shifty's defaults."""
from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

from rdflib import BNode, Graph
from rdflib.namespace import RDF, SH

from bask_223p.validate import shacl
from bask_223p.validate.shacl import validate_223p


def _pin(monkeypatch, shapes: Path) -> None:
    shapes.write_text("pinned-223p\n", encoding="utf-8")
    monkeypatch.setattr(
        shacl,
        "PINNED_223P_SHA256",
        hashlib.sha256(shapes.read_bytes()).hexdigest(),
    )


def _install_engines(monkeypatch, *, missing: list[str] | None = None, shifty_conforms: bool = True):
    calls: dict = {}

    class StubEnv:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def add(self, path, **kwargs):
            calls.setdefault("add", []).append(kwargs)

        def missing_imports(self, root):
            return list(missing or [])

        def copy_closure(self, root):
            graph = Graph()
            graph.add((BNode(), RDF.type, SH.NodeShape))
            return graph, [root]

    def fake_validate(data, shapes, **opts):
        calls["shifty"] = opts
        return shifty_conforms, Graph(), "shifty-report"

    def pyshacl_validate(**kwargs):
        calls["pyshacl"] = kwargs
        return False, Graph(), "pyshacl-report"

    monkeypatch.setitem(sys.modules, "ontoenv", types.SimpleNamespace(OntoEnv=StubEnv))
    monkeypatch.setitem(sys.modules, "shifty", types.SimpleNamespace(validate=fake_validate))
    monkeypatch.setitem(
        sys.modules,
        "pyshacl",
        types.SimpleNamespace(validate=pyshacl_validate),
    )
    return calls


def test_validate_223p_uses_the_offline_closure_and_shifty_defaults(tmp_path, monkeypatch):
    shapes = tmp_path / "223p.ttl"
    model = tmp_path / "model.ttl"
    model.write_text("@prefix ex: <http://example.org/> .\n", encoding="utf-8")
    _pin(monkeypatch, shapes)
    calls = _install_engines(monkeypatch)

    result = validate_223p(model, shapes_path=shapes)

    assert result["conforms"] is True
    assert result["engine"] == "shifty"
    assert result["minimum_severity"] == "Violation"
    assert calls["init"] == {"temporary": True, "offline": True, "strict": True}
    assert calls["shifty"] == {"minimum_severity": "Violation"}
    assert calls["add"][0]["fetch_imports"] is False
    assert "pyshacl" not in calls


def test_unresolved_imports_are_an_error(tmp_path, monkeypatch):
    shapes = tmp_path / "223p.ttl"
    model = tmp_path / "model.ttl"
    model.write_text("@prefix ex: <http://example.org/> .\n", encoding="utf-8")
    _pin(monkeypatch, shapes)
    _install_engines(monkeypatch, missing=["http://qudt.org/3.4.0/shacl/qudt-all"])
    import pytest

    with pytest.raises(ValueError, match="Unresolved owl:imports"):
        validate_223p(model, shapes_path=shapes)


def test_pyshacl_cross_check_does_not_set_conforms(tmp_path, monkeypatch):
    shapes = tmp_path / "223p.ttl"
    model = tmp_path / "model.ttl"
    model.write_text("@prefix ex: <http://example.org/> .\n", encoding="utf-8")
    _pin(monkeypatch, shapes)
    calls = _install_engines(monkeypatch, shifty_conforms=True)

    result = validate_223p(model, shapes_path=shapes, cross_check="pyshacl")

    assert result["conforms"] is True
    assert result["cross_check"]["engine"] == "pyshacl"
    assert result["cross_check"]["conforms"] is False
    assert calls["pyshacl"]["advanced"] is True
