"""Contract/parity tests: Niagara baskStream vs BACpypes3 scan TTL, units, sidecars, and NullProxyExt."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from bask_223p.bacnet.export223 import (
    QK,
    QUDT,
    UNIT,
    build_223p_graphs,
    export_223p,
    snapshot_from_points,
    validate_223p_references,
    validate_official_223p,
)
from bask_223p.bacnet.refs import (
    BACNET,
    BRICK_REF,
    NIAGARA,
    S223,
    BacnetPoint,
    load_points_scan,
    niagara_object_uri,
    niagara_reference_iri,
    reference_iri,
)
from bask_223p.bacnet.units import implied_unit_for_bacnet_property, resolve_point_unit_and_quantity_kind, resolve_qudt_unit, resolve_unit_and_quantity_kind

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://example.org/parity"
SHAPES = Path(os.environ["S223_SHAPES"]) if os.environ.get("S223_SHAPES") else None


def test_shared_units_helper_caches_and_resolves_both_formats():
    resolve_qudt_unit.cache_clear()
    resolve_unit_and_quantity_kind.cache_clear()

    assert resolve_unit_and_quantity_kind("°F") == ("DEG_F", "Temperature")
    assert resolve_unit_and_quantity_kind("http://data.ashrae.org/bacnet/2024#EngineeringUnits.degrees-fahrenheit") == (
        "DEG_F",
        "Temperature",
    )
    assert resolve_unit_and_quantity_kind("EngineeringUnits.cubic-feet-per-minute") == ("FT3-PER-MIN", "VolumeFlowRate")
    assert resolve_unit_and_quantity_kind("percent-relative-humidity") == ("PERCENT_RH", "RelativeHumidity")
    assert resolve_unit_and_quantity_kind("null") == (None, None)

    # Niagara inches-of-water-column spellings
    for raw in ("in/wc", "inH2O", "inH₂O", "in. W.C.", "in.w.c.", "inches of water column", "inches-of-water"):
        assert resolve_unit_and_quantity_kind(raw) == ("IN_H2O", "Pressure"), raw

    # BACnet File file-size is octets; official QUDT unit:BYTE / quantitykind:ByteDataVolume
    assert implied_unit_for_bacnet_property("file", "file-size") == ("BYTE", "ByteDataVolume")
    assert implied_unit_for_bacnet_property("file", "42@bacnet:bacnet-property-identifier") == ("BYTE", "ByteDataVolume")
    assert implied_unit_for_bacnet_property("analog-input", "present-value") == (None, None)
    assert resolve_point_unit_and_quantity_kind("null", "file", "file-size") == ("BYTE", "ByteDataVolume")
    assert resolve_point_unit_and_quantity_kind("", "file", "42") == ("BYTE", "ByteDataVolume")
    # A resolved facet unit wins over the implied BACnet file-size unit
    assert resolve_point_unit_and_quantity_kind("byte", "file", "file-size") == ("BYTE", "ByteDataVolume")

    # Second call hits lru_cache
    assert resolve_qudt_unit("°F") == "DEG_F"
    assert resolve_qudt_unit.cache_info().hits >= 1


def test_load_points_scan_preserves_units_and_snapshot_from_points_keeps_them():
    pts = load_points_scan(ROOT / "examples" / "bench-scan.ttl")
    by_id = {(p.device_instance, p.object_id): p for p in pts}
    sat = by_id[(10001, "analog-input,1")]
    assert sat.qudt_unit == "DEG_F"
    assert sat.quantity_kind == "Temperature"
    airflow = by_id[(10002, "analog-input,6")]
    assert airflow.qudt_unit == "FT3-PER-MIN"
    assert airflow.quantity_kind == "VolumeFlowRate"

    snap = snapshot_from_points(pts, station_label="bench-scan.ttl")
    dev1 = next(d for d in snap["devices"] if d["device_instance"] == 10001)
    assert dev1["bacnet_device_name"] == "LabAHU_01"
    sat_snap = next(p for p in dev1["points"] if p["bacnet"]["object_identifier"] == "analog-input,1")
    assert sat_snap["qudt_unit"] == "DEG_F"
    assert sat_snap["quantity_kind"] == "Temperature"
    assert sat_snap["bacnet"]["object_name"] == "SA-T"


def test_niagara_and_bacpypes3_scan_parity_identical_223p_and_sidecar_isolation(tmp_path):
    scan_ttl = tmp_path / "scan.ttl"
    scan_ttl.write_text(
        """
        @prefix bacnet: <http://data.ashrae.org/bacnet/2024#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

        <bacnet://10001> a bacnet:Device ;
            bacnet:device-instance "10001"^^xsd:nonNegativeInteger ;
            bacnet:device-address [ rdfs:label "192.0.2.55" ; bacnet:mac-address "deadbeef" ] ;
            bacnet:contains <bacnet://10001/device,10001>, <bacnet://10001/analog-input,1>, <bacnet://10001/analog-value,1> .

        <bacnet://10001/device,10001> a bacnet:DeviceObject, bacnet:Object ;
            bacnet:object-identifier "device,10001" ;
            bacnet:object-name "LabAHU_01" ;
            bacnet:object-type bacnet:ObjectType.device .

        <bacnet://10001/analog-input,1> a bacnet:AnalogInputObject, bacnet:Object ;
            bacnet:object-identifier "analog-input,1" ;
            bacnet:object-name "SA-T" ;
            bacnet:description "Supply Air Temp" ;
            bacnet:object-type bacnet:ObjectType.analog-input ;
            bacnet:units bacnet:EngineeringUnits.degrees-fahrenheit ;
            bacnet:present-value 55.4 .

        <bacnet://10001/analog-value,1> a bacnet:AnalogValueObject, bacnet:Object ;
            bacnet:object-identifier "analog-value,1" ;
            bacnet:object-name "SAT_SP" ;
            bacnet:object-type bacnet:ObjectType.analog-value ;
            bacnet:units bacnet:EngineeringUnits.degrees-fahrenheit .
        """,
        encoding="utf-8",
    )

    niagara_snap = {
        "format": "baskstream-discovery-snapshot",
        "version": 1,
        "devices": [
            {
                "ord": "slot:/Drivers/BacnetNetwork/NiagaraDisplayAHU",
                "name": "NiagaraDisplayAHU",
                "typeSpec": "bacnet:BacnetDevice",
                "device_instance": 10001,
                "bacnet_device_name": "LabAHU_01",
                "points": [
                    {
                        "ord": "slot:/Drivers/BacnetNetwork/NiagaraDisplayAHU/points/SA_Temp_Display",
                        "name": "SA_Temp_Display",
                        "display": "Supply Air Temp (Niagara)",
                        "typeSpec": "control:NumericPoint",
                        "proxyExtType": "bacnet:BacnetProxyExt",
                        "valueKind": "numeric",
                        "writable": False,
                        "facets": {"units": "°F", "precision": 1},
                        "bacnet": {
                            "object_type": "analog-input",
                            "object_instance": 1,
                            "object_identifier": "analog-input,1",
                            "object_name": "SA-T",
                            "description": "Supply Air Temp",
                            "property_identifier": "present-value",
                        },
                    },
                    {
                        "ord": "slot:/Drivers/BacnetNetwork/NiagaraDisplayAHU/points/SAT_SP",
                        "name": "SAT_SP",
                        "display": "SAT_SP",
                        "typeSpec": "control:NumericWritable",
                        "proxyExtType": "bacnet:BacnetProxyExt",
                        "valueKind": "numeric",
                        "writable": True,
                        "facets": {"units": "°F", "precision": 1},
                        "bacnet": {
                            "object_type": "analog-value",
                            "object_instance": 1,
                            "object_identifier": "analog-value,1",
                            "object_name": "SAT_SP",
                            "property_identifier": "present-value",
                        },
                    },
                ],
            }
        ],
    }
    snap_path = tmp_path / "niagara-snap.json"
    import json
    snap_path.write_text(json.dumps(niagara_snap), encoding="utf-8")

    n_ttl = tmp_path / "niagara-223p.ttl"
    s_ttl = tmp_path / "scan-223p.ttl"
    n_sum = export_223p(base=BASE, out=n_ttl, snapshot_path=snap_path)
    s_sum = export_223p(base=BASE, out=s_ttl, input_path=scan_ttl)

    g_n = Graph().parse(n_ttl, format="turtle")
    g_s = Graph().parse(s_ttl, format="turtle")

    # Core parity contract: identical ref IRIs, typings, QUDT unit/QuantityKind, and 223P triples!
    assert set(g_n) == set(g_s)
    assert n_sum["triples"] == s_sum["triples"]

    ref = reference_iri(BASE, 10001, "analog-input,1")
    assert (ref, RDF.type, S223.BACnetExternalReference) in g_n
    assert (ref, BRICK_REF.BACnetURI, Literal("bacnet://10001/analog-input,1")) in g_n
    pt = next(g_n.subjects(S223.hasExternalReference, ref))
    assert (pt, RDF.type, S223.QuantifiableObservableProperty) in g_n
    assert (pt, QUDT.hasUnit, UNIT.DEG_F) in g_n
    assert (pt, QUDT.hasQuantityKind, QK.Temperature) in g_n

    # Niagara extras live in .niagara.ttl, NOT in 223P TTL
    assert (None, NIAGARA.slotPath, None) not in g_n
    assert (None, NIAGARA.facets, None) not in g_n
    assert (None, NIAGARA.proxyExtType, None) not in g_n
    ng = Graph().parse(n_ttl.with_suffix(".niagara.ttl"), format="turtle")
    assert (pt, NIAGARA.slotPath, Literal("slot:/Drivers/BacnetNetwork/NiagaraDisplayAHU/points/SA_Temp_Display")) in ng
    assert (pt, NIAGARA.proxyExtType, Literal("bacnet:BacnetProxyExt")) in ng

    # Raw scan TTL is kept as sidecar, NOT mixed into 223P TTL
    scan_ttl_text = s_ttl.read_text(encoding="utf-8")
    assert "192.0.2.55" not in scan_ttl_text
    assert "deadbeef" not in scan_ttl_text
    assert "55.4" not in scan_ttl_text
    sidecar = Path(s_sum["scan_sidecar_output"])
    assert sidecar.is_file()
    assert "192.0.2.55" in sidecar.read_text(encoding="utf-8")

    # SHACL results equal
    assert validate_223p_references(g_n) == validate_223p_references(g_s)
    if SHAPES and SHAPES.is_file():
        r_n = validate_official_223p(g_n, SHAPES)
        r_s = validate_official_223p(g_s, SHAPES)
        assert r_n["conforms"] == r_s["conforms"] is True
        assert r_n["violation_count"] == r_s["violation_count"] == 0
        assert r_n["warning_count"] == r_s["warning_count"]


def test_null_proxy_ext_points_get_niagara_ref_and_zero_shacl_violations():
    snap = {
        "format": "baskstream-discovery-snapshot",
        "version": 1,
        "devices": [
            {
                "ord": "slot:/Drivers/FEC_Proxys",
                "name": "FEC_Proxys",
                "device_instance": None,
                "points": [
                    {
                        "ord": "slot:/Drivers/FEC_Proxys/proxy_OA_T",
                        "name": "proxy_OA_T",
                        "display": "proxy_OA_T",
                        "typeSpec": "control:NumericWritable",
                        "valueKind": "numeric",
                        "writable": True,
                        "facets": {"units": "null", "precision": 1},
                    }
                ],
            }
        ],
    }
    g, ng, bacnet_pts, summary = build_223p_graphs(snap, BASE)
    assert bacnet_pts == []
    assert summary["unbound_points"] == 1
    assert summary["niagara_only_references"] == 1

    n_uri = niagara_object_uri("slot:/Drivers/FEC_Proxys/proxy_OA_T")
    n_ref = niagara_reference_iri(BASE, "slot:/Drivers/FEC_Proxys/proxy_OA_T")
    assert n_uri == "niagara://slot/Drivers/FEC_Proxys/proxy_OA_T"
    assert (n_ref, RDF.type, NIAGARA.NiagaraExternalReference) in g
    assert (n_ref, RDF.type, S223.ExternalReference) in g
    assert (n_ref, NIAGARA.niagaraURI, Literal(n_uri)) in g
    assert (n_ref, NIAGARA.noBacnetSource, Literal(True, datatype=XSD.boolean)) in g

    pt = next(g.subjects(S223.hasExternalReference, n_ref))
    assert (pt, RDF.type, S223.ActuatableProperty) in g
    assert (pt, RDF.type, S223.QuantifiableProperty) not in g
    assert (pt, NIAGARA.proxyExtType, Literal("NullProxyExt")) in ng

    val = validate_223p_references(g)
    assert val["conforms"] is True
    assert val["niagara_external_references"] == 1
    assert val["warnings"][0]["warning_class"] == "NoBacnetSource"

    if SHAPES and SHAPES.is_file():
        off = validate_official_223p(g, SHAPES)
        assert off["conforms"] is True
        assert off["violation_count"] == 0, off["violations"]

def test_in_wc_and_filesize_units_in_223p_graph():
    """Niagara in/wc and BACnet file-size emit official QUDT terms, not QuantifiableProperty violations."""
    snap = {
        "format": "baskstream-discovery-snapshot",
        "version": 1,
        "devices": [
            {
                "ord": "slot:/Drivers/BacnetNetwork/Bench",
                "name": "Bench",
                "device_instance": 5007,
                "bacnet_device_name": "Bench",
                "points": [
                    {
                        "ord": "slot:/Drivers/BacnetNetwork/Bench/points/DUCT-P",
                        "name": "DUCT-P",
                        "display": "DUCT-P",
                        "typeSpec": "control:NumericPoint",
                        "valueKind": "numeric",
                        "writable": False,
                        "facets": {"units": "in/wc", "precision": 2},
                        "bacnet": {
                            "object_type": "analog-input",
                            "object_instance": 9334,
                            "object_identifier": "analog-input,9334",
                            "property_identifier": "85@bacnet:bacnet-property-identifier",
                        },
                    },
                    {
                        "ord": "slot:/Drivers/BacnetNetwork/Bench/points/Boot-fileSize",
                        "name": "Boot-fileSize",
                        "display": "8-1.Boot-fileSize",
                        "typeSpec": "control:NumericPoint",
                        "valueKind": "numeric",
                        "writable": False,
                        "facets": {"units": "null", "precision": 1},
                        "bacnet": {
                            "object_type": "file",
                            "object_instance": 1,
                            "object_identifier": "file,1",
                            "property_identifier": "42@bacnet:bacnet-property-identifier",
                        },
                    },
                ],
            }
        ],
    }
    g, _ng, bacnet_pts, summary = build_223p_graphs(snap, BASE)
    assert summary["bacnet_bound_points"] == 2
    assert summary["qudt_units_unmapped"] == []

    by_oid = {p.object_id: p for p in bacnet_pts}
    assert by_oid["analog-input,9334"].qudt_unit == "IN_H2O"
    assert by_oid["analog-input,9334"].quantity_kind == "Pressure"
    assert by_oid["analog-input,9334"].property_identifier == "present-value"
    assert by_oid["file,1"].qudt_unit == "BYTE"
    assert by_oid["file,1"].quantity_kind == "ByteDataVolume"
    assert by_oid["file,1"].property_identifier == "file-size"

    duct = next(s for s in g.subjects(RDF.type, S223.QuantifiableObservableProperty)
                if (s, QUDT.hasUnit, UNIT.IN_H2O) in g)
    assert (duct, QUDT.hasQuantityKind, QK.Pressure) in g
    fsz = next(s for s in g.subjects(RDF.type, S223.QuantifiableObservableProperty)
               if (s, QUDT.hasUnit, UNIT.BYTE) in g)
    assert (fsz, QUDT.hasQuantityKind, QK.ByteDataVolume) in g
    # Official terms only — do not invent a local quantity kind
    assert (fsz, QUDT.hasQuantityKind, QK.ByteDataVolume) in g

    # Scan-path BacnetPoint agrees (shared model)
    scan_pt = BacnetPoint(
        device_instance=5007,
        object_type="file",
        object_instance=1,
        object_name="Boot-fileSize",
        property_identifier="42@bacnet:bacnet-property-identifier",
        unit_raw="null",
    )
    assert scan_pt.property_identifier == "file-size"
    assert scan_pt.qudt_unit == "BYTE"
    assert scan_pt.quantity_kind == "ByteDataVolume"

    if SHAPES and SHAPES.is_file():
        off = validate_official_223p(g, SHAPES)
        assert off["violation_count"] == 0, off["violations"]
        assert off["conforms"] is True

