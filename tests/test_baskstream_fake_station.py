"""Integration tests: read-only baskStream client + export-223p against a fake API 1.14 station.

The fake station (tests/fake_baskstream_station.py) speaks the real wire protocol:
Niagara SCRAM-SHA-256 login, session cookie, RFC 6455 WebSocket with MessagePack
frames, and Niagara-shaped BACnet metadata (``config/deviceObject`` and
``<point>/proxyExt`` objectIds). No real station is contacted.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("msgpack")
pytest.importorskip("websocket")
pytest.importorskip("requests")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_baskstream_station import FakeBaskStreamStation  # noqa: E402
from rdflib import Graph  # noqa: E402
from rdflib.namespace import RDF  # noqa: E402

from bask_223p.bacnet.baskstream import (  # noqa: E402
    READ_ONLY_OPS,
    BaskStreamError,
    ReadOnlyBaskStreamClient,
    coerce_facets,
    discover_station_snapshot,
    map_qudt_unit,
)
from bask_223p.bacnet.export223 import export_223p  # noqa: E402
from bask_223p.bacnet.refs import BACNET, BRICK_REF, S223  # noqa: E402
from bask_223p.cli import main  # noqa: E402

AHU = "slot:/Drivers/BacnetNetwork/LabAHU_01"


def _client(st: FakeBaskStreamStation, **kw) -> ReadOnlyBaskStreamClient:
    c = ReadOnlyBaskStreamClient(st.url, timeout=kw.pop("timeout", 5), **kw)
    c.login(st.user, st.password)
    c.connect()
    return c


def test_scram_login_verifies_server_signature():
    with FakeBaskStreamStation() as st, _client(st) as c:
        assert c.server_signature_verified is True
        assert c.health()["authenticatedUser"] == st.user


def test_scram_rejects_tampered_server_signature():
    with FakeBaskStreamStation(tamper_server_signature=True) as st:
        c = ReadOnlyBaskStreamClient(st.url, timeout=5)
        with pytest.raises(BaskStreamError, match="server signature verification failed"):
            c.login(st.user, st.password)
        c.close()


def test_scram_missing_signature_allowed_unless_required():
    with FakeBaskStreamStation(omit_server_signature=True) as st:
        with ReadOnlyBaskStreamClient(st.url, timeout=5) as c:
            c.login(st.user, st.password)
            assert c.server_signature_verified is False
        with ReadOnlyBaskStreamClient(st.url, timeout=5, require_server_signature=True) as c:
            with pytest.raises(BaskStreamError, match="no server signature"):
                c.login(st.user, st.password)


def test_scram_wrong_password_fails_without_leaking_it():
    with FakeBaskStreamStation() as st:
        with ReadOnlyBaskStreamClient(st.url, timeout=5) as c:
            with pytest.raises(BaskStreamError) as err:
                c.login(st.user, "not-the-password")
            assert "not-the-password" not in str(err.value)


def test_capabilities_reports_api_1_14_and_operations():
    with FakeBaskStreamStation() as st, _client(st) as c:
        caps = c.capabilities()["capabilities"]
        assert caps["apiVersion"] == "1.14"
        assert c.api_version == "1.14"
        assert c.supports("resolve") and c.supports("describe_component")
        assert not c.supports("made_up_op")


def test_search_describe_and_describe_component_shapes():
    with FakeBaskStreamStation() as st, _client(st) as c:
        res = c.search("slot:/Drivers", features=["point"], metadata="full")["result"]
        assert res["count"] == 9 and res["truncated"] is False
        node = next(n for n in res["nodes"] if n["name"] == "SA_Temp")
        assert node["metadata"]["classification"]["isProxyPoint"] is True
        assert node["metadata"]["facets"]["units"] == "°F"

        desc = c.describe(node["slotPath"])["node"]
        assert desc["metadata"]["driver"]["device"] == AHU

        dev = c.describe_component(f"{AHU}/config/deviceObject")["result"]
        assert {s["name"] for s in dev["slots"]} >= {"objectId", "objectName"}
        proxy = c.describe_component(f"{node['slotPath']}/proxyExt")["result"]
        oid = next(s for s in proxy["slots"] if s["name"] == "objectId")
        assert oid["value"]["encoded"] == "analogInput:1"


def test_read_tags_and_resolve_helpers():
    with FakeBaskStreamStation() as st, _client(st) as c:
        tags = c.read_tags([AHU, f"{AHU}/points/SA_Temp"], dictionary="hs")
        assert [t["ok"] for t in tags["targets"]] == [True, True]
        assert {t["id"] for t in tags["targets"][0]["tags"]} == {"hs:equip", "hs:ahu"}
        res = c.resolve(AHU, role="discharge air temp sensor")
        assert res["inferred"] is True
        assert res["matches"][0]["ord"].endswith("/SA_Temp")
        with pytest.raises(ValueError):
            c.resolve(AHU)
        with pytest.raises(ValueError):
            c.read_tags([f"slot:/x{i}" for i in range(101)])


def test_discovery_extracts_object_ids_and_leaves_unbound_points_unbound():
    with FakeBaskStreamStation() as st:
        snap = discover_station_snapshot(st.url, username=st.user, password=st.password, timeout=5)
    assert snap["apiVersion"] == "1.14"
    assert snap["scramServerSignatureVerified"] is True
    devs = {d["name"]: d for d in snap["devices"]}
    assert devs["LabAHU_01"]["device_instance"] == 10001
    assert devs["LabVAV_02"]["device_instance"] == 10002
    pts = {p["name"]: p for d in snap["devices"] for p in d["points"]}
    assert pts["SA_Temp"]["bacnet"]["object_identifier"] == "analog-input,1"
    assert pts["Occ_Mode"]["bacnet"]["object_identifier"] == "multi-state-value,3"
    assert pts["SAT_SP"]["bacnet"]["priority_for_writing"] == 16  # proxyExt writePriority
    assert pts["Fan_Cmd"]["bacnet"]["priority_for_writing"] == 10  # active level fallback
    assert "bacnet" not in pts["Calc_Avg"]  # no proxyExt -> never invented
    assert pts["Occ_Mode"]["valueKind"] == "enum"
    assert any("no BACnet proxyExt objectId" in w for w in snap["warnings"])


def test_client_never_sends_write_ops_and_refuses_them_locally():
    with FakeBaskStreamStation() as st:
        discover_station_snapshot(st.url, username=st.user, password=st.password, timeout=5)
        assert st.state.write_attempts == []
        assert set(st.state.received_ops) <= READ_ONLY_OPS
        with _client(st) as c:
            for op in ("write", "write_tags", "apply_model_changes", "revert_change", "commit_upload"):
                with pytest.raises(ValueError, match="forbidden in read-only client"):
                    c.call(op, ord=AHU)
        assert st.state.write_attempts == []


def test_non_terminal_notices_are_recorded_and_surface_as_warnings():
    notices = {"search": [
        {"op": "resync_required", "reason": "event_backlog", "droppedEvents": 10},
        {"op": "subscriptions_revoked", "points": [], "reason": "authorization_revoked"},
    ]}
    with FakeBaskStreamStation(notices_before=notices) as st:
        snap = discover_station_snapshot(st.url, username=st.user, password=st.password, timeout=5)
    joined = " | ".join(snap["warnings"])
    assert "resync_required" in joined and "subscriptions_revoked" in joined


@pytest.mark.parametrize("kw,match", [({"revoke_on": "search"}, "session_revoked"),
                                      ({"timeout_on": "search"}, "request_timeout")])
def test_terminal_notices_abort_discovery(kw, match):
    with FakeBaskStreamStation(**kw) as st:
        with pytest.raises(BaskStreamError, match=match):
            discover_station_snapshot(st.url, username=st.user, password=st.password, timeout=5)


def test_unanswered_request_times_out_instead_of_hanging():
    with FakeBaskStreamStation(silent_on="ping") as st, _client(st, timeout=1) as c:
        with pytest.raises(BaskStreamError, match="timed out"):
            c.ping()


def test_require_authorization_header():
    with FakeBaskStreamStation(require_auth_header=True) as st:
        with pytest.raises(Exception, match="403"):
            discover_station_snapshot(st.url, username=st.user, password=st.password, timeout=5)
        snap = discover_station_snapshot(st.url, username=st.user, password=st.password,
                                         timeout=5, ws_auth_header=True)
        assert len(snap["devices"]) == 2
        assert any("Authorization" in h for h in st.state.ws_handshake_headers)
    assert st.password not in json.dumps(snap)


def test_export_223p_end_to_end_from_live_station_validates(tmp_path):
    out = tmp_path / "site-223p.ttl"
    snap_path = tmp_path / "snapshot.json"
    with FakeBaskStreamStation() as st:
        env = tmp_path / "station.env"
        env.write_text(f"NIAGARA_USER={st.user}\nNIAGARA_PASS={st.password}\n", encoding="utf-8")
        summary = export_223p(base="https://example.org/lab", out=out, station=st.url, env_file=env,
                              manifest=tmp_path / "bindings.csv", validate_out=tmp_path / "validate",
                              save_snapshot=snap_path)
        assert st.state.write_attempts == []
    assert summary["api_version"] == "1.14"
    assert summary["devices"] == 2 and summary["points"] == 9
    assert summary["bacnet_bound_points"] == 8 and summary["unbound_points"] == 1
    assert summary["validation"]["conforms"] is True
    assert summary["validation"]["bacnet_external_references"] == 8
    g = Graph().parse(out, format="turtle")
    assert len(list(g.subjects(RDF.type, S223.BACnetExternalReference))) == 8
    assert len(list(g.subjects(RDF.type, S223.Equipment))) == 2
    dev_ids = {str(o) for o in g.objects(None, BACNET["device-identifier"])}
    assert dev_ids == {"device,10001", "device,10002"}
    for f in [out, snap_path, *tmp_path.joinpath("validate").iterdir(), tmp_path / "bindings.csv"]:
        assert st.password not in f.read_text(encoding="utf-8")
    # The saved snapshot reproduces the same graph offline.
    again = export_223p(base="https://example.org/lab", out=tmp_path / "again.ttl", snapshot_path=snap_path)
    assert again["triples"] == summary["triples"]


def test_cli_export_223p_live(tmp_path, capsys):
    with FakeBaskStreamStation() as st:
        env = tmp_path / "station.env"
        env.write_text(f"NIAGARA_USER={st.user}\nNIAGARA_PASS={st.password}\n", encoding="utf-8")
        rc = main(["export-223p", "--station", st.url, "--env-file", str(env),
                   "--base", "https://example.org/lab", "--out", str(tmp_path / "x.ttl")])
    printed = capsys.readouterr().out
    assert rc == 0
    assert st.password not in printed
    assert json.loads(printed)["validation"]["conforms"] is True


def test_unit_and_facet_shapes_from_real_stations():
    assert map_qudt_unit("fahrenheit;°F;(K)*1.8+-459.67") == "DEG_F"
    assert map_qudt_unit("u:percent;%") == "PERCENT"
    assert map_qudt_unit("cubic feet per minute;cfm;(m3)*0.00047194745/(s)") == "FT3-PER-MIN"
    assert map_qudt_unit("furlongs per fortnight") is None
    assert coerce_facets({"units": "°F", "precision": "1"}) == {"units": "°F", "precision": 1}
    assert coerce_facets("units=u:°F|precision=i:1") == {"units": "°F", "precision": 1}
    assert coerce_facets(None) == {}


@pytest.mark.skipif(not os.environ.get("BASK_DEMO_URL"), reason="set BASK_DEMO_URL to a running `bask demo --serve`")
def test_against_running_bask_demo(tmp_path):
    """Optional: the upstream `bask demo` simulator (user admin / password demo by default)."""
    url = os.environ["BASK_DEMO_URL"]
    user = os.environ.get("BASK_DEMO_USER", "admin")
    pw = os.environ.get("BASK_DEMO_PASS", "demo")
    snap = discover_station_snapshot(url, username=user, password=pw, timeout=20)
    assert snap["devices"], "demo station returned no devices"
    env = tmp_path / "demo.env"
    env.write_text(f"NIAGARA_USER={user}\nNIAGARA_PASS={pw}\n", encoding="utf-8")
    summary = export_223p(base="https://example.org/demo", out=tmp_path / "demo.ttl", station=url,
                          env_file=env, infer_demo_ids=True)
    assert summary["validation"]["conforms"] is True
    assert summary["bacnet_bound_points"] == summary["points"]


# ---------------------------------------------------------------- API 1.12 (signed upstream jar)

def test_api_1_12_station_exports_the_same_graph(tmp_path):
    """The signed upstream jar is API 1.12; discovery and export must not depend on 1.13/1.14 ops."""
    results = {}
    for version in ("1.12", "1.14"):
        with FakeBaskStreamStation(api_version=version) as st:
            env = tmp_path / f"{version}.env"
            env.write_text(f"NIAGARA_USER={st.user}\nNIAGARA_PASS={st.password}\n", encoding="utf-8")
            out = tmp_path / f"site-{version}.ttl"
            summary = export_223p(base="https://example.org/lab", out=out, station=st.url, env_file=env)
            assert st.state.write_attempts == []
            assert "resolve" not in st.state.received_ops
            results[version] = (summary, Graph().parse(out, format="turtle"))
    s12, g12 = results["1.12"]
    s14, g14 = results["1.14"]
    assert s12["api_version"] == "1.12"
    assert s12["bacnet_bound_points"] == s14["bacnet_bound_points"] == 8
    assert s12["validation"]["conforms"] is True
    refs = lambda g: {str(o) for o in g.objects(None, BRICK_REF.BACnetURI)}  # noqa: E731
    assert refs(g12) == refs(g14)


def test_api_1_12_resolve_is_gated_on_capabilities():
    with FakeBaskStreamStation(api_version="1.12") as st, _client(st) as c:
        caps = c.capabilities()["capabilities"]
        assert caps["apiVersion"] == "1.12" and "resolve" not in caps["operations"]
        assert c.supports("search") and c.supports("describe_component") and c.supports("read_tags")
        assert not c.supports("resolve")
        sent = len(st.state.received_ops)
        with pytest.raises(BaskStreamError, match=r"resolve needs baskStream API 1\.14\+ \(station reports 1\.12\)"):
            c.resolve("slot:/Drivers/BacnetNetwork/LabAHU_01", role="discharge-air-temp")
        assert len(st.state.received_ops) == sent  # refused locally, nothing sent


def test_subscribe_cov_and_unsubscribe():
    pt = f"{AHU}/points/SA_Temp"
    with FakeBaskStreamStation(cov_on_subscribe=True) as st, _client(st) as c:
        sub = c.subscribe([pt])
        assert sub["op"] == "subscribed"
        assert sub["points"][0]["ok"] is True
        assert sub["points"][0]["value"] == pytest.approx(55.2)

        evt = c.recv_event(timeout=2.0)
        assert evt is not None and evt["op"] == "cov"
        assert evt["points"][0]["point"] == pt

        status = c.subscription_status()
        assert status["subscribedCount"] == 1

        unsub = c.unsubscribe([pt])
        assert unsub["op"] == "unsubscribed"
        assert c.subscription_status()["subscribedCount"] == 0
        assert c.recv_event(timeout=0.05) is None
