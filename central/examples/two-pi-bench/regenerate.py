"""Rewrite the worked example from central.two_pi. Run from the repo root:

    python central/examples/two-pi-bench/regenerate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "central"))

from central.merge import stable_source_turtle, stable_turtle, stable_trig  # noqa: E402
from central.two_pi import build  # noqa: E402

HERE = Path(__file__).resolve().parent
SHAPES = ROOT / "src" / "bask_223p" / "ontologies" / "223p" / "223p.ttl"
ONTOLOGY = ROOT / "src" / "bask_223p" / "ontologies" / "223p" / "closure"


def main() -> None:
    bacnet, niagara, merged, report, validation = build(SHAPES, ONTOLOGY)
    (HERE / "edge1").mkdir(exist_ok=True)
    (HERE / "edge2").mkdir(exist_ok=True)
    (HERE / "edge1" / "model.ttl").write_text(stable_source_turtle(bacnet), encoding="utf-8")
    (HERE / "edge2" / "model.ttl").write_text(stable_source_turtle(niagara), encoding="utf-8")
    (HERE / "unified.ttl").write_text(stable_turtle(merged.union), encoding="utf-8")
    (HERE / "unified.trig").write_text(stable_trig(merged), encoding="utf-8")
    (HERE / "reconcile.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (HERE / "validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    data_union = merged.data_union_triples
    counts = {
        "edge1": len(bacnet),
        "edge2": len(niagara),
        "sum": len(bacnet) + len(niagara),
        "data_union": data_union,
        "collapsed": len(bacnet) + len(niagara) - data_union,
        "union_with_provenance": len(merged.union),
        "provenance": len(merged.union) - data_union,
    }
    (HERE / "counts.json").write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
