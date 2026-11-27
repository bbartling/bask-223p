"""The worked two-Pi example stays deterministic and collapses shared IRIs."""
from __future__ import annotations

import json
from pathlib import Path

from rdflib import Graph, Literal

from central.merge import skolemize, stable_nt, stable_source_turtle, stable_turtle
from central.two_pi import build

HERE = Path(__file__).resolve().parents[1] / "examples" / "two-pi-bench"
SHAPES = Path(__file__).resolve().parents[2] / "bask_223p" / "ontologies" / "223p" / "223p.ttl"
ONTOLOGY = SHAPES.parent / "closure"


def test_two_pi_outputs_match_the_builder_and_are_deterministic():
    bacnet, niagara, merged, report, validation = build(SHAPES, ONTOLOGY)
    again, _niagara, merged_again, report_again, _validation_again = build()
    assert stable_source_turtle(bacnet) == stable_source_turtle(again)
    assert stable_nt(skolemize(bacnet, "bench", "pi-bacnet")) == stable_nt(skolemize(again, "bench", "pi-bacnet"))
    assert stable_nt(merged.union) == stable_nt(merged_again.union)
    assert report == report_again

    on_disk = Graph()
    on_disk.parse(HERE / "unified.ttl", format="turtle")
    assert set(on_disk) == set(merged.union)
    assert (HERE / "unified.ttl").read_text(encoding="utf-8") == stable_turtle(merged.union)
    assert json.loads((HERE / "reconcile.json").read_text(encoding="utf-8")) == report
    assert json.loads((HERE / "validation.json").read_text(encoding="utf-8")) == validation

    assert (HERE / "edge1" / "model.ttl").read_text(encoding="utf-8") == stable_source_turtle(bacnet)
    assert (HERE / "edge2" / "model.ttl").read_text(encoding="utf-8") == stable_source_turtle(niagara)
    assert b"_:b0" in (HERE / "edge1" / "model.ttl").read_bytes()

    counts = json.loads((HERE / "counts.json").read_text(encoding="utf-8"))
    assert counts["edge1"] == len(bacnet)
    assert counts["edge2"] == len(niagara)
    assert counts["data_union"] < counts["sum"]
    assert counts["collapsed"] == counts["sum"] - counts["data_union"]
    assert counts["data_union"] == merged.data_union_triples
    assert counts["union_with_provenance"] == len(merged.union)

    pair = report["pairs"][0]
    kinds = {row["bacnet_uri"]: row["kind"] for row in pair["rows"]}
    assert kinds["bacnet://5007/analog-input,1"] == "duplicate-collapsed"
    assert kinds["bacnet://5007/analog-input,2"] == "duplicate-collapsed"
    assert kinds["bacnet://5007/analog-input,3"] == "conflicting"
    assert kinds["bacnet://5007/analog-input,4"] == "conflicting"
    assert kinds["bacnet://5007/analog-output,1"] == "only_in_left"
    assert kinds["bacnet://5007/analog-input,5"] == "only_in_right"
    ma = next(row for row in pair["rows"] if row["object_identifier"] == "analog-input,3")
    assert ma["left_unit"] == "DEG_C"
    assert ma["right_unit"] == "DEG_F"
    duct = next(row for row in pair["rows"] if row["object_identifier"] == "analog-input,4")
    assert duct["left_name"] == "DAP-P"
    assert duct["right_name"] == "DuctStatic"
    assert (None, None, Literal("bacnet://5007/analog-input,1")) in merged.union
    assert sum(1 for _ in merged.union.triples((None, None, Literal("bacnet://5007/analog-input,1")))) == 1
