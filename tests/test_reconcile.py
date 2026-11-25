"""Scan <-> Niagara reconcile and shared-reference alignment (BACpypes3 RDF shape)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from rdflib import Graph, URIRef

from fake_baskstream_station import FakeBaskStreamStation
from bask_223p.bacnet.export223 import export_223p, validate_223p_references
from bask_223p.bacnet.reconcile import run_reconcile
from bask_223p.bacnet.refs import (
    BACNET,
    BRICK_REF,
    build_reference_graph,
    load_points,
    reference_iri,
)
from bask_223p.cli import main

ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "examples" / "bench-scan.ttl"
BASE = "https://example.org/lab"


@pytest.fixture(scope="module")
def niagara(tmp_path_factory):
    out = tmp_path_factory.mktemp("niagara")
    with FakeBaskStreamStation() as st:
        env = out / "station.env"
        env.write_text(f"NIAGARA_USER={st.user}\nNIAGARA_PASS={st.password}\n", encoding="utf-8")
        export_223p(base=BASE, out=out / "site-223p.ttl", station=st.url, env_file=env,
                    save_snapshot=out / "snapshot.json")
    env.unlink()
    return out


def _identity(graph: Graph, ref) -> set:
    preds = (BACNET["object-identifier"], BACNET["device-identifier"], BACNET["property-identifier"],
             BRICK_REF.BACnetURI, BACNET.objectOf)
    return {(p, o) for p in preds for o in graph.objects(ref, p)}


def test_scan_and_niagara_share_reference_iris_and_identity(niagara):
    n_graph = Graph().parse(niagara / "site-223p.ttl")
    scan_points = [p for p in load_points(SCAN) if p.object_type not in ("device", "file")]
    s_graph, _ = build_reference_graph(scan_points, BASE)
    ref = URIRef(reference_iri(BASE, 10001, "analog-value,1"))
    assert (ref, None, None) in n_graph and (ref, None, None) in s_graph
    assert _identity(n_graph, ref) == _identity(s_graph, ref)
    assert str(n_graph.value(ref, BRICK_REF.BACnetURI)) == "bacnet://10001/analog-value,1"
    assert str(n_graph.value(ref, BACNET["device-identifier"])) == "device,10001"
    # Niagara display names are never asserted as BACnet object-name; the scan's real name is.
    assert n_graph.value(ref, BACNET["object-name"]) is None
    assert str(s_graph.value(ref, BACNET["object-name"])) == "SAT_SP"
    # Same device node on both sides
    dev = n_graph.value(ref, BACNET.objectOf)
    assert dev == s_graph.value(ref, BACNET.objectOf) == URIRef("bacnet://10001")


def test_object_node_matches_bacpypes3_rdf_shape(niagara):
    from rdflib.namespace import RDF
    from bask_223p.bacnet.refs import BACNET_2024
    g = Graph().parse(niagara / "site-223p.ttl")
    scan = Graph().parse(SCAN)
    obj = URIRef("bacnet://10001/analog-value,1")
    for pred in (BACNET_2024["object-identifier"], BACNET_2024["object-type"]):
        assert set(g.objects(obj, pred)) == set(scan.objects(obj, pred))
    assert (URIRef("bacnet://10001"), BACNET_2024.contains, obj) in g
    assert (obj, RDF.type, BACNET_2024.Object) in g


def test_reconcile_ttl_vs_scan(niagara, tmp_path):
    merged = tmp_path / "merged.ttl"
    result = run_reconcile(niagara / "site-223p.ttl", SCAN, tmp_path / "r", merged_out=merged)
    assert (result["matched"], result["missing_in_niagara"], result["missing_in_scan"]) == (7, 2, 1)
    assert result["devices_only_in_scan"] == [10003]
    assert result["devices_only_in_niagara"] == []
    assert result["ignored_scan_object_types"] == {"file": 1}
    assert result["name_differs"] == 1 and result["unit_differs"] == 1
    assert result["device_name_differs"] == 0
    rows = {(r["device_instance"], r["object_identifier"]): r
            for r in csv.DictReader((tmp_path / "r" / "reconcile.csv").open(encoding="utf-8"))}
    assert rows[("10001", "binary-input,1")]["status"] == "missing_in_scan"
    assert rows[("10001", "analog-value,99")]["status"] == "missing_in_niagara"
    assert rows[("10001", "analog-input,1")]["name_differs"] == "True"
    assert rows[("10001", "analog-output,2")]["unit_differs"] == "True"
    # Merged graph: one ref per object, still a valid 223P reference set
    assert result["merged"]["validation"]["conforms"], result["merged"]["validation"]["violations"]
    g = Graph().parse(merged)
    uris = [str(o) for o in g.objects(None, BRICK_REF.BACnetURI)]
    assert len(uris) == len(set(uris)) == 10


def test_reconcile_snapshot_json_matches_ttl(niagara, tmp_path):
    a = run_reconcile(niagara / "site-223p.ttl", SCAN, tmp_path / "a")
    b = run_reconcile(niagara / "snapshot.json", SCAN, tmp_path / "b")
    keys = ("matched", "missing_in_niagara", "missing_in_scan", "devices_only_in_scan")
    assert {k: a[k] for k in keys} == {k: b[k] for k in keys}


def test_reconcile_scan_csv(niagara, tmp_path):
    csv_path = tmp_path / "scan.csv"
    csv_path.write_text(
        "DeviceID,ObjType,ObjInst,Name,Description,Units\n"
        "10001,analog-value,1,SAT_SP,,degrees-fahrenheit\n"
        "10002,analog-input,6,AIRFLOW,,cubic-feet-per-minute\n"
        "10001,file,1,CONFIG,,\n",
        encoding="utf-8",
    )
    r = run_reconcile(niagara / "site-223p.ttl", csv_path, tmp_path / "r")
    assert r["matched"] == 2 and r["missing_in_niagara"] == 0 and r["missing_in_scan"] == 6


def test_merged_conflicting_device_name_is_flagged(niagara, tmp_path):
    scan = tmp_path / "scan.ttl"
    scan.write_text(SCAN.read_text(encoding="utf-8").replace('"LabAHU_01"', '"OTHER-NAME"'), encoding="utf-8")
    r = run_reconcile(niagara / "site-223p.ttl", scan, tmp_path / "r", merged_out=tmp_path / "m.ttl")
    assert r["device_name_differs"] == 1
    assert not r["merged"]["validation"]["conforms"]
    assert any("device-name" in v["message"] for v in r["merged"]["validation"]["violations"])


def test_validator_rejects_two_object_names():
    g, _ = build_reference_graph([p for p in load_points(SCAN) if p.object_type == "analog-value"], BASE)
    ref = URIRef(reference_iri(BASE, 10001, "analog-value,1"))
    from rdflib import Literal
    g.add((ref, BACNET["object-name"], Literal("SECOND")))
    v = validate_223p_references(g)
    assert not v["conforms"]


def test_cli_match_alias_and_strict(niagara, tmp_path, capsys):
    rc = main(["match", "--niagara", str(niagara / "site-223p.ttl"), "--scan", str(SCAN),
               "--out", str(tmp_path / "r")])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["matched"] == 7 and "rows" not in out
    rc = main(["reconcile", "--niagara", str(niagara / "site-223p.ttl"), "--scan", str(SCAN),
               "--out", str(tmp_path / "r2"), "--strict"])
    assert rc == 1


def test_no_secrets_in_outputs(niagara, tmp_path):
    run_reconcile(niagara / "snapshot.json", SCAN, tmp_path / "r")
    for f in list((tmp_path / "r").iterdir()) + list(niagara.iterdir()):
        text = f.read_text(encoding="utf-8")
        assert "NIAGARA_PASS" not in text and "password" not in text.lower()
