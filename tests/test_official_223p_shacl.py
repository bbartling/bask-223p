"""Official 223P SHACL conformance for export-223p output.

Skipped unless S223_SHAPES points at a 223p.ttl (for example the file at
https://open223.info/223p.ttl). Warnings about a missing Sensor that observes a
property are expected for an external-reference graph; Violations are not.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from bask_223p.bacnet.export223 import export_223p

SHAPES = Path(os.environ["S223_SHAPES"]) if os.environ.get("S223_SHAPES") else None
pytestmark = pytest.mark.skipif(not (SHAPES and SHAPES.is_file()), reason="set S223_SHAPES to a 223p.ttl")


def test_snapshot_export_has_no_official_223p_violations(tmp_path):
    summary = export_223p(
        base="https://example.org/lab",
        out=tmp_path / "site-223p.ttl",
        snapshot_path=Path(__file__).resolve().parents[1] / "examples" / "baskstream-ahu-snapshot.json",
        official_shapes=SHAPES,
        validate_out=tmp_path / "validate",
    )
    official = summary["official_223p"]
    assert official["violation_count"] == 0, official["violations"]
    assert official["conforms"] is True
    # External-reference graphs are not a full 223 topology; Sensor warnings are expected.
    assert official["warning_count"] >= 1
    assert any("observes" in (w.get("message") or "") for w in official["warnings"])
    assert (tmp_path / "validate" / "official-223p-shacl.json").is_file()
