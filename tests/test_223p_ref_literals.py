"""BACnet reference literals stay inside the 223P datatype rules."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from rdflib import Graph, Literal
from rdflib.namespace import XSD

from bask_223p.bacnet.export223 import export_223p
from bask_223p.bacnet.refs import BACNET

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "examples" / "baskstream-ahu-snapshot.json"


def test_snapshot_refs_use_one_non_negative_write_priority(tmp_path):
    out = tmp_path / "site.ttl"
    export_223p(base="https://example.org/lab", out=out, snapshot_path=SNAPSHOT)
    graph = Graph().parse(out, format="turtle")

    integers = [
        (str(subj), str(pred), str(obj))
        for subj, pred, obj in graph
        if isinstance(obj, Literal) and obj.datatype == XSD.integer
    ]
    assert integers == []

    priorities: Counter = Counter()
    for subj, _pred, obj in graph.triples((None, BACNET["priority-for-writing"], None)):
        priorities[subj] += 1
        assert obj.datatype == XSD.nonNegativeInteger
    assert priorities
    assert max(priorities.values()) == 1

    instances = list(graph.triples((None, BACNET["device-instance"], None)))
    assert instances
    assert all(obj.datatype == XSD.nonNegativeInteger for _subj, _pred, obj in instances)
