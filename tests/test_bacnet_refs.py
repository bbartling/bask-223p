"""BACnet device/object identifier export as Brick and 223P references."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from bask_223p.bacnet.refs import (
    IDENT_PATTERN,
    BACNET,
    BACNET_2024,
    BRICK_REF,
    S223,
    build_reference_graph,
    export_bacnet_refs,
    load_points_csv,
    load_points_scan,
    normalize_object_type,
    normalize_property_identifier,
    object_identifier,
)
from bask_223p.cli import main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "fec-test-bench-points.csv"
ILLUSTRATIVE = ROOT / "examples" / "illustrative-ahu-points.csv"

BACNET_2020 = "http://data.ashrae.org/bacnet/2020#"


def test_normalize_object_type_aliases():
    assert normalize_object_type("analogInput") == "analog-input"
    assert normalize_object_type("analog_input") == "analog-input"
    assert normalize_object_type("AI") == "analog-input"
    assert normalize_object_type("multiStateValue") == "multi-state-value"
    assert normalize_property_identifier("presentValue") == "present-value"
    assert normalize_property_identifier("85@bacnet:bacnet-property-identifier") == "present-value"
    assert normalize_property_identifier("42@bacnet:bacnet-property-identifier") == "file-size"
    assert normalize_property_identifier("fileSize") == "file-size"
    assert object_identifier("analogValue", 2) == "analog-value,2"
    assert IDENT_PATTERN.fullmatch("analog-input,2")
    assert not IDENT_PATTERN.fullmatch("analog-input,0")


def test_example_export_has_brick_and_223p_bindings():
    points = load_points_csv(EXAMPLE)
    assert len(points) == 20
    graph, summary = build_reference_graph(points, "https://example.org/lab")
    assert summary["points"] == 20
    assert summary["devices"] == 2
    assert summary["approved_brick_classes"] == 0

    # Sanitized example: no personal names or IPv4 addresses.
    ttl = graph.serialize(format="turtle")
    assert "203.0.113.9" not in ttl and "aabbcc" not in ttl
    assert "secret-host" not in ttl
    assert "LabAHU" in ttl and "LabSchedule" in ttl

    sa_t = next(p for p in points if p.object_name == "SA-T")
    assert sa_t.device_instance == 3456789
    assert sa_t.object_id == "analog-input,2"
    assert sa_t.bacnet_uri == "bacnet://3456789/analog-input,2"

    ref_count = len(set(graph.subjects(RDF.type, BRICK_REF.BACnetReference)))
    assert ref_count == 20
    assert len(set(graph.subjects(RDF.type, S223.BACnetExternalReference))) == 20

    refs = list(graph.subjects(RDF.type, BRICK_REF.BACnetReference))
    identified = False
    for ref in refs:
        oid = str(graph.value(ref, BACNET["object-identifier"]))
        if oid == "analog-input,2":
            identified = True
            assert str(graph.value(ref, BACNET["object-name"])) == "SA-T"
            assert graph.value(ref, BACNET.objectOf) == URIRef("bacnet://3456789")
            assert str(graph.value(ref, BRICK_REF.BACnetURI)) == "bacnet://3456789/analog-input,2"
            assert str(graph.value(ref, BACNET["device-identifier"])) == "device,3456789"
            assert str(graph.value(ref, BACNET["device-name"])) == "LabAHU"
            assert str(graph.value(ref, BACNET["property-identifier"])) == "present-value"
            assert (None, BRICK_REF.hasExternalReference, ref) in graph
            assert (None, S223.hasExternalReference, ref) in graph
    assert identified

    device = URIRef("bacnet://3456789")
    assert (device, RDF.type, BACNET.BACnetDevice) in graph
    assert (device, BACNET.contains, URIRef("bacnet://3456789/analog-input,2")) in graph
    assert (URIRef("bacnet://3456789/analog-input,2"), BACNET_2024["object-identifier"],
            Literal("analog-input,2")) in graph


def test_csv_aliases_and_duplicate_rejected(tmp_path):
    src = tmp_path / "points.csv"
    src.write_text(
        "DeviceID,ObjType,ObjInst,Name\n"
        "1001,analogInput,7,SA-T\n",
        encoding="utf-8",
    )
    points = load_points_csv(src)
    assert points[0].object_type == "analog-input"
    assert points[0].object_id == "analog-input,7"
    src.write_text(
        "device_instance,object_type,object_instance,object_name\n"
        "1001,analog-input,7,SA-T\n"
        "1001,analog-input,7,RA-T\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate BACnet object"):
        load_points_csv(src)


def test_approved_brick_class_and_instance_zero(tmp_path):
    src = tmp_path / "points.csv"
    src.write_text(
        "device_instance,object_type,object_instance,object_name,brick_class,haystack_tags\n"
        "12,analog-input,4,SA-T,https://brickschema.org/schema/Brick#Supply_Air_Temperature_Sensor,point sensor\n"
        "0,binary-value,1,flag,,\n",
        encoding="utf-8",
    )
    points = load_points_csv(src)
    graph, summary = build_reference_graph(points, "https://example.org/lab")
    assert summary["approved_brick_classes"] == 1
    assert list(graph.subjects(
        RDF.type, URIRef("https://brickschema.org/schema/Brick#Supply_Air_Temperature_Sensor")))
    zero_ref = None
    for ref in graph.subjects(RDF.type, S223.BACnetExternalReference):
        if str(graph.value(ref, BACNET["object-name"])) == "flag":
            zero_ref = ref
    assert zero_ref is not None
    # device instance 0 does not satisfy 223P device-identifier pattern
    assert graph.value(zero_ref, BACNET["device-identifier"]) is None
    assert str(graph.value(zero_ref, BRICK_REF.BACnetURI)) == "bacnet://0/binary-value,1"


def test_load_scan_ttl_skips_device_objects_and_addresses(tmp_path):
    scan = tmp_path / "scan.ttl"
    scan.write_text(
        """
        @prefix bacnet: <http://data.ashrae.org/bacnet/2024#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

        <bacnet://1001> a bacnet:Device ;
            bacnet:device-instance "1001"^^xsd:nonNegativeInteger ;
            bacnet:contains <bacnet://1001/analog-input,2>,
                <bacnet://1001/device,1001>,
                <bacnet://1001/network-port,1> ;
            bacnet:device-address [ rdfs:label "203.0.113.9" ; bacnet:mac-address "aabbcc" ] .

        <bacnet://1001/analog-input,2>
            bacnet:object-identifier "analog-input,2" ;
            bacnet:object-name "SA-T" ;
            bacnet:description "Supply Air Temperature" ;
            bacnet:present-value 72.1 .

        <bacnet://1001/device,1001>
            bacnet:object-identifier "device,1001" ;
            bacnet:object-name "LabAHU" .

        <bacnet://1001/network-port,1>
            bacnet:object-identifier "network-port,1" ;
            bacnet:object-name "Port-1" .
        """,
        encoding="utf-8",
    )
    points = load_points_scan(scan)
    assert [p.object_name for p in points] == ["SA-T"]
    assert points[0].device_name == "LabAHU"
    graph, _ = build_reference_graph(points, "https://example.org/lab")
    ttl = graph.serialize(format="turtle")
    assert "203.0.113.9" not in ttl
    assert "aabbcc" not in ttl
    assert "72.1" not in ttl
    assert "bacnet://1001/analog-input,2" in ttl


def test_cli_export_writes_ttl_and_manifest(tmp_path):
    out = tmp_path / "refs.ttl"
    manifest = tmp_path / "bindings.csv"
    assert main([
        "export-bacnet-refs",
        "--input", str(EXAMPLE),
        "--base", "https://example.org/lab",
        "--out", str(out),
        "--manifest", str(manifest),
    ]) == 0
    assert out.exists()
    graph = Graph().parse(out, format="turtle")
    assert len(set(graph.subjects(RDF.type, BRICK_REF.BACnetReference))) == 20
    rows = list(csv.DictReader(manifest.open(encoding="utf-8")))
    assert len(rows) == 20
    sat = next(r for r in rows if r["object_name"] == "SA-T")
    assert sat["device_instance"] == "3456789"
    assert sat["object_identifier"] == "analog-input,2"
    assert sat["bacnet_uri"] == "bacnet://3456789/analog-input,2"
    payload = json.loads(manifest.with_suffix(".json").read_text(encoding="utf-8"))
    assert len(payload) == 20


def test_export_bindings_roundtrip_from_ttl(tmp_path):
    from bask_223p.bacnet.refs import export_bindings, bindings_from_graph
    ttl = tmp_path / "refs.ttl"
    export_bacnet_refs(EXAMPLE, "https://example.org/lab", ttl)
    table = tmp_path / "bindings.csv"
    result = export_bindings(ttl, table)
    assert result["bindings"] == 20
    rows = list(csv.DictReader(table.open(encoding="utf-8")))
    sat = next(r for r in rows if r["object_name"] == "SA-T")
    assert sat["device_instance"] == "3456789"
    assert sat["object_identifier"] == "analog-input,2"
    assert sat["bacnet_uri"] == "bacnet://3456789/analog-input,2"
    assert sat["haystack_tags"] == "point sensor temp"
    graph = Graph().parse(ttl, format="turtle")
    assert len(bindings_from_graph(graph)) == 20


def test_haystack_checker_ok_and_failures(tmp_path):
    from bask_223p.bacnet.haystack import (
        check_grid, load_expected, load_grid, parse_zinc, run_check,
    )
    expected = load_expected(EXAMPLE)
    # Binding table itself is not a haystack grid; use the example fixture.
    fixture = ROOT / "examples" / "fec-haystack-grid.json"
    actual = load_grid(fixture)
    summary = check_grid(expected, actual)
    assert summary["passed"] is True
    assert summary["ok"] == 20
    assert summary["missing"] == 0

    # Zinc parsing + a missing and wrong-binding case
    zinc = tmp_path / "grid.zinc"
    zinc.write_text(
        'ver:"3.0"\n'
        "id,dis,point,bacnetCur,tags\n"
        '@p:SA-T,"SA-T",M,"bacnet://3456789/analog-input,99","point sensor temp"\n'
        '@p:OA-T,"OA-T",M,"bacnet://3456789/analog-input,6","point sensor temp"\n',
        encoding="utf-8",
    )
    subset = [row for row in expected if row["object_name"] in {"SA-T", "OA-T", "RA-T"}]
    failed = check_grid(subset, load_grid(zinc))
    assert failed["passed"] is False
    by_name = {f["object_name"]: f for f in failed["findings"]}
    assert by_name["RA-T"]["status"] == "missing"
    assert by_name["SA-T"]["status"] == "wrong_binding"
    assert by_name["OA-T"]["status"] == "ok"
    assert parse_zinc(zinc.read_text(encoding="utf-8"))

    out = tmp_path / "check"
    result = run_check(EXAMPLE, out, grid_path=fixture)
    assert result["passed"] is True
    assert (out / "haystack-check.csv").exists()


def test_cli_export_bindings_and_check_haystack(tmp_path):
    ttl = tmp_path / "refs.ttl"
    manifest = tmp_path / "bindings.csv"
    assert main([
        "export-bacnet-refs", "--input", str(EXAMPLE),
        "--base", "https://example.org/lab", "--out", str(ttl), "--manifest", str(manifest),
    ]) == 0
    table = tmp_path / "from-ttl.csv"
    assert main(["export-bindings", "--data", str(ttl), "--out", str(table)]) == 0
    assert len(list(csv.DictReader(table.open(encoding="utf-8")))) == 20
    check_out = tmp_path / "hs"
    fixture = ROOT / "examples" / "fec-haystack-grid.json"
    assert main([
        "check-haystack", "--expected", str(table),
        "--grid", str(fixture), "--out", str(check_out),
    ]) == 0


def test_cli_check_haystack_live_login_stops(tmp_path, monkeypatch):
    from bask_223p.bacnet import haystack as hc

    def fake_fetch(url, timeout=8.0, env_file=None, insecure=False, username=None, password=None):
        return None, {
            "base_url": url, "about_status": 302, "read_status": 302,
            "reachable": False, "login_required": True,
            "detail": "Haystack REST redirected to the station login. No credentials were sent.",
        }

    monkeypatch.setattr(hc, "fetch_haystack_grid", fake_fetch)
    out = tmp_path / "live"
    code = main([
        "check-haystack", "--expected", str(EXAMPLE),
        "--url", "https://localhost/haystack", "--out", str(out),
    ])
    assert code == 2
    payload = json.loads((out / "haystack-check.json").read_text(encoding="utf-8"))
    assert payload["login_required"] is True
    assert payload["reachable"] is False


def test_illustrative_ahu_emits_223p_external_references():
    points = load_points_csv(ILLUSTRATIVE)
    assert len(points) == 10
    assert {p.device_instance for p in points} == {10001}
    assert {p.device_name for p in points} == {"IllustrativeAHU"}
    by_name = {p.object_name: p for p in points}
    assert by_name["SAT"].object_id == "analog-input,12"
    assert by_name["RAT"].object_id == "analog-input,13"
    assert by_name["MAT"].object_id == "analog-input,14"
    assert by_name["OAT"].object_id == "analog-input,15"
    assert by_name["SF-S"].object_id == "binary-input,1"
    assert by_name["SF-O"].object_id == "analog-output,1"
    assert by_name["CHW-VLV"].object_id == "analog-output,2"
    assert by_name["HW-VLV"].object_id == "analog-output,3"
    assert by_name["OA-DMPR"].object_id == "analog-output,4"
    assert by_name["SAT-SP"].object_id == "analog-value,1"
    assert by_name["SAT-SP"].priority_for_writing == 16
    assert all(p.property_identifier == "present-value" for p in points)
    assert all(p.property_array_index is None for p in points)

    graph, summary = build_reference_graph(points, "https://example.org/illustrative-ahu")
    assert summary["points"] == 10 and summary["devices"] == 1
    assert len(set(graph.subjects(RDF.type, S223.BACnetExternalReference))) == 10
    sat_ref = None
    for ref in graph.subjects(RDF.type, S223.BACnetExternalReference):
        if str(graph.value(ref, BACNET["object-name"])) == "SAT":
            sat_ref = ref
            break
    assert sat_ref is not None
    assert (None, S223.hasExternalReference, sat_ref) in graph
    assert str(graph.value(sat_ref, BACNET["device-identifier"])) == "device,10001"
    assert str(graph.value(sat_ref, BACNET["device-name"])) == "IllustrativeAHU"
    assert str(graph.value(sat_ref, BACNET["object-identifier"])) == "analog-input,12"
    assert str(graph.value(sat_ref, BACNET["object-name"])) == "SAT"
    assert str(graph.value(sat_ref, BACNET["property-identifier"])) == "present-value"
    assert graph.value(sat_ref, BACNET["property-array-index"]) is None
    sat_sp = next(r for r in graph.subjects(RDF.type, S223.BACnetExternalReference)
                  if str(graph.value(r, BACNET["object-name"])) == "SAT-SP")
    assert int(graph.value(sat_sp, BACNET["priority-for-writing"])) == 16
    shipped = (ROOT / "examples" / "illustrative-ahu-refs.ttl").read_text(encoding="utf-8")
    assert shipped.lower().startswith("# illustrative")
    assert "s223:BACnetExternalReference" in shipped
    assert "bacnet:device-identifier" in shipped
    assert "device,10001" in shipped


def test_optional_property_array_index_and_invalid_priority(tmp_path):
    src = tmp_path / "idx.csv"
    src.write_text(
        "device_instance,object_type,object_instance,object_name,property_array_index,priority_for_writing\n"
        "10001,analog-value,1,PriorityArray,2,8\n",
        encoding="utf-8",
    )
    point = load_points_csv(src)[0]
    assert point.property_array_index == 2
    assert point.priority_for_writing == 8
    graph, _ = build_reference_graph([point], "https://example.org/illustrative-ahu")
    ref = next(graph.subjects(RDF.type, S223.BACnetExternalReference))
    assert int(graph.value(ref, BACNET["property-array-index"])) == 2
    assert int(graph.value(ref, BACNET["priority-for-writing"])) == 8
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "device_instance,object_type,object_instance,object_name,priority_for_writing\n"
        "10001,analog-value,1,SAT-SP,17\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="priority-for-writing"):
        load_points_csv(bad)


def test_load_env_credentials_from_file(tmp_path, monkeypatch):
    from bask_223p.bacnet.haystack import load_env_credentials
    env = tmp_path / 'station.env'
    env.write_text('NIAGARA_USER=demo-user\nNIAGARA_PASS=demo-pass-1\n', encoding='utf-8')
    # Process env must not leak into the file-derived values when keys are present
    from bask_223p.bacnet.haystack import ENV_PASS_KEYS, ENV_USER_KEYS
    for key in (*ENV_USER_KEYS, *ENV_PASS_KEYS):
        monkeypatch.delenv(key, raising=False)
    user, password = load_env_credentials(env)
    assert user == 'demo-user'
    assert password == 'demo-pass-1'
    missing = tmp_path / 'empty.env'
    missing.write_text('# comment only\n', encoding='utf-8')
    user, password = load_env_credentials(missing)
    assert user is None and password is None
