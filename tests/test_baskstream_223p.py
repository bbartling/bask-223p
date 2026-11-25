"""Tests for read-only baskStream discovery and external ASHRAE 223P + Brick export."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from rdflib import Graph
from rdflib.namespace import RDF

from bask_223p.bacnet.baskstream import (
    ReadOnlyBaskStreamClient,
    discover_station_snapshot,
    map_qudt_unit,
    parse_bacnet_object_id,
    parse_facets_string,
)
from bask_223p.bacnet.export223 import (
    QUDT,
    UNIT,
    build_223p_graph,
    export_223p,
    validate_223p_references,
)
from bask_223p.bacnet.refs import BACNET, BRICK_REF, S223, bindings_from_graph
from bask_223p.cli import main

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "examples" / "baskstream-ahu-snapshot.json"
CACHE_223P = ROOT / "ontology_cache" / "223p.ttl"


def test_read_only_client_blocks_write_ops():
    client = ReadOnlyBaskStreamClient("https://localhost", insecure=True)
    for forbidden in ("write", "write_tags", "write_relations", "apply_model_changes", "ack_alarm", "write_file"):
        with pytest.raises(ValueError, match="forbidden in read-only client"):
            client.call(forbidden, ord="slot:/Drivers")


def test_parse_bacnet_id_facets_and_qudt_units():
    assert parse_bacnet_object_id("analogInput:1173") == ("analog-input", 1173)
    assert parse_bacnet_object_id("device:5007") == ("device", 5007)
    assert parse_bacnet_object_id("multiStateValue:3") == ("multi-state-value", 3)
    assert parse_facets_string("units=u:°F|precision=i:1") == {"units": "°F", "precision": 1}
    assert map_qudt_unit("°F") == "DEG_F"
    assert map_qudt_unit("inH₂O") == "IN_H2O"
    assert map_qudt_unit("%RH") == "PERCENT_RH"
    assert map_qudt_unit("null") is None


def test_export_223p_from_snapshot_with_catalog_and_validation(tmp_path):
    out_ttl = tmp_path / "site-223p.ttl"
    manifest = tmp_path / "bindings.csv"
    val_dir = tmp_path / "validate"
    catalogs = {"ASHRAE223": CACHE_223P} if CACHE_223P.is_file() else None

    summary = export_223p(
        base="https://example.org/lab",
        out=out_ttl,
        snapshot_path=SNAPSHOT,
        catalogs=catalogs,
        manifest=manifest,
        validate_out=val_dir,
    )
    assert summary["devices"] == 1
    assert summary["points"] == 6
    assert summary["bacnet_bound_points"] == 6
    assert summary["unbound_points"] == 0
    assert summary["qudt_units_mapped"] == 5
    assert summary["validation"]["conforms"] is True
    assert summary["validation"]["bacnet_external_references"] == 6

    g = Graph().parse(out_ttl, format="turtle")
    assert len(list(g.subjects(RDF.type, S223.Equipment))) == 1
    assert len(list(g.subjects(RDF.type, S223.QuantifiableObservableProperty))) == 3
    assert len(list(g.subjects(RDF.type, S223.QuantifiableActuatableProperty))) == 2
    assert len(list(g.subjects(RDF.type, S223.EnumeratedObservableProperty))) == 1
    assert (None, QUDT.hasUnit, UNIT.DEG_F) in g
    assert (None, QUDT.hasUnit, UNIT.IN_H2O) in g
    assert (None, QUDT.hasUnit, UNIT.PERCENT) in g

    # Confirm 223P §11.18.1 BACnetExternalReference + Clause Q.8 URI + priority-for-writing
    refs = list(g.subjects(RDF.type, S223.BACnetExternalReference))
    assert len(refs) == 6
    from bask_223p.bacnet.refs import VOCAB
    sat_sp_pt = next(p for p in g.subjects(VOCAB.object_name, None) if str(g.value(p, VOCAB.object_name)) == "SAT_SP")
    sat_sp_ref = g.value(sat_sp_pt, S223.hasExternalReference)
    # Niagara point names are not BACnet object names: never asserted as bacnet:object-name.
    assert g.value(sat_sp_ref, BACNET["object-name"]) is None
    assert str(g.value(sat_sp_ref, BRICK_REF.BACnetURI)) == "bacnet://10001/analog-value,1"
    assert str(g.value(sat_sp_ref, BACNET["device-identifier"])) == "device,10001"
    assert int(str(g.value(sat_sp_ref, BACNET["priority-for-writing"]))) == 16

    # Round-trip bindings
    rows = bindings_from_graph(g)
    assert len(rows) == 6
    assert (val_dir / "validation-223p.json").is_file()
    cov = json.loads((val_dir / "coverage-223p.json").read_text(encoding="utf-8"))
    assert cov["points_analyzed"] == 6
    # Never auto-assign Brick/223P semantic classes from lexical hints
    assert summary["approved_classes_applied"] == 0


def test_unbound_points_do_not_invent_fake_bacnet_refs():
    snap = {
        "format": "baskstream-discovery-snapshot",
        "version": 1,
        "devices": [
            {
                "ord": "slot:/Drivers/BacnetNetwork/DemoAHU",
                "name": "DemoAHU",
                "device_instance": 1001,
                "points": [
                    {
                        "ord": "slot:/Drivers/BacnetNetwork/DemoAHU/points/SAT",
                        "name": "SAT",
                        "display": "SupplyAirTemp",
                        "valueKind": "numeric",
                        "writable": False,
                        "facets": {"units": "°F"},
                    },
                    {
                        "ord": "slot:/Drivers/BacnetNetwork/DemoAHU/points/FanCmd",
                        "name": "FanCmd",
                        "display": "FanCmd",
                        "valueKind": "boolean",
                        "writable": True,
                        "facets": {},
                    },
                ],
            }
        ],
    }
    g_strict, pts_strict, sum_strict = build_223p_graph(snap, "https://example.org/demo", infer_demo_ids=False)
    assert sum_strict["bacnet_bound_points"] == 0
    assert sum_strict["unbound_points"] == 2
    assert pts_strict == []
    assert validate_223p_references(g_strict)["conforms"] is True

    g_demo, pts_demo, sum_demo = build_223p_graph(snap, "https://example.org/demo", infer_demo_ids=True)
    assert sum_demo["bacnet_bound_points"] == 2
    assert sum_demo["synthetic_bacnet_ids"] == 2
    assert len(pts_demo) == 2
    assert validate_223p_references(g_demo)["conforms"] is True


def test_discover_station_snapshot_extracts_niagara_device_and_proxy_ids(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def login(self, u, p):
            return {"ok": True, "apiVersion": "1.14"}

        def connect(self):
            return None

        def ping(self):
            return {"op": "pong"}

        def capabilities(self):
            return {"capabilities": {"apiVersion": "1.14", "module": "baskStream"}}

        def browse(self, base, depth=1, metadata="full"):
            return {"node": {"ord": base, "children": []}}

        def search(self, base, features=None, metadata="full", limit=1000):
            if features == ["point"]:
                return {
                    "result": {
                        "nodes": [
                            {
                                "ord": "slot:/Drivers/BacnetNetwork/Unit1/points/OA_T",
                                "name": "OA_T",
                                "display": "OA-T",
                                "typeSpec": "control:NumericPoint",
                                "writable": False,
                                "metadata": {
                                    "point": {"facets": {"units": "°F"}, "writable": False},
                                    "tags": [{"id": "hs:temp"}, {"id": "n:point"}],
                                },
                            }
                        ]
                    }
                }
            return {
                "result": {
                    "nodes": [
                        {
                            "ord": "slot:/Drivers/BacnetNetwork/Unit1",
                            "name": "Unit1",
                            "display": "Unit1",
                            "typeSpec": "bacnet:BacnetDevice",
                            "metadata": {"classification": {"isDriverDevice": True}},
                        }
                    ]
                }
            }

        def describe(self, ord_value, metadata="full"):
            return {"node": {"ord": ord_value, "name": "Unit1", "display": "Unit1", "typeSpec": "bacnet:BacnetDevice", "metadata": {"tags": []}}}

        def describe_component(self, ord_value):
            if ord_value.endswith("/config/deviceObject"):
                return {"result": {"slots": [{"name": "objectId", "value": {"encoded": "device:5007"}}]}}
            if ord_value.endswith("/proxyExt"):
                return {
                    "result": {
                        "slots": [
                            {"name": "objectId", "value": {"encoded": "analogInput:1173"}},
                            {"name": "propertyId", "value": {"encoded": "presentValue"}},
                        ]
                    }
                }
            return {"result": {"slots": []}}

    monkeypatch.setattr("bask_223p.bacnet.baskstream.ReadOnlyBaskStreamClient", FakeClient)
    snap = discover_station_snapshot("https://localhost", username="reader", password="x", insecure=True)
    assert snap["apiVersion"] == "1.14"
    dev = snap["devices"][0]
    assert dev["device_instance"] == 5007
    pt = dev["points"][0]
    assert pt["bacnet"]["object_identifier"] == "analog-input,1173"
    assert pt["bacnet"]["property_identifier"] == "present-value"
    assert pt["haystackTags"] == ["temp"]


def test_cli_export_223p(tmp_path):
    out_ttl = tmp_path / "cli-223p.ttl"
    val_dir = tmp_path / "val"
    rc = main([
        "export-223p",
        "--snapshot", str(SNAPSHOT),
        "--base", "https://example.org/lab",
        "--out", str(out_ttl),
        "--validate-out", str(val_dir),
    ])
    assert rc == 0
    assert out_ttl.is_file()
    assert (val_dir / "validation-223p.json").is_file()
