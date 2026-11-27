"""SPARQL is on unless EDGE_ENABLE_SPARQL is explicitly off."""
from __future__ import annotations

from edge_microdot.cli import _sparql_enabled
from edge_microdot.config import LoadedConfig
from edge_microdot.share import export_config_document


def test_unset_sparql_is_on_and_zero_turns_it_off(monkeypatch):
    monkeypatch.delenv("EDGE_ENABLE_SPARQL", raising=False)
    cfg = LoadedConfig(site="demo")
    assert _sparql_enabled(cfg) is True
    assert export_config_document(cfg, [], environ={})["sparql_enabled"] is True

    monkeypatch.setenv("EDGE_ENABLE_SPARQL", "0")
    assert _sparql_enabled(cfg) is False
    assert export_config_document(cfg, [], environ={"EDGE_ENABLE_SPARQL": "0"})["sparql_enabled"] is False

    monkeypatch.setenv("EDGE_ENABLE_SPARQL", "false")
    assert _sparql_enabled(cfg) is False

    monkeypatch.setenv("EDGE_ENABLE_SPARQL", "1")
    assert _sparql_enabled(cfg) is True
    assert export_config_document(cfg, [], environ={"EDGE_ENABLE_SPARQL": "1"})["sparql_enabled"] is True
