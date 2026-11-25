"""External ASHRAE 223P + Brick reference export from baskStream discovery or inventories.

Turns discovered Niagara devices, points, facets, and BACnet IDs into an external
ASHRAE 223P Turtle graph with ``s223:BACnetExternalReference`` (223P 2nd public
review draft, section 11.18.1), Brick ``ref:BACnetReference`` + ``ref:BACnetURI``
(Clause Q.8 ``bacnet://<device>/<type>,<instance>``), QUDT units from facets,
and ``s223:Equipment`` / ``s223:Property`` subclasses.

No station writes occur. Reviewed Brick/223P classes are applied only from an
explicit approved mappings file, never from lexical similarity.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from ..audit.coverage import find_matches, label_index
from ..model.graph import load_approved_mappings, valid_external_iri
from .baskstream import (
    decode_n4_slot_text,
    discover_station_snapshot,
    load_baskstream_snapshot,
    map_qudt_unit,
    parse_bacnet_object_id,
)
from .refs import (
    BACNET,
    BACNET_2024,
    BRICK_REF,
    IDENT_PATTERN,
    S223,
    VOCAB,
    BacnetPoint,
    add_bacnet_device,
    add_bacnet_reference,
    bindings_from_graph,
    load_points,
    normalize_object_type,
    normalize_property_identifier,
    write_manifest,
)

QUDT = Namespace("http://qudt.org/schema/qudt/")
UNIT = Namespace("http://qudt.org/vocab/unit/")


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", (text or "").strip()).strip("-")
    return cleaned or "item"


def _equip_node(base: str, device_ord: str, device_name: str, device_instance: int | None) -> URIRef:
    key = device_ord or f"device:{device_instance}:{device_name}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return URIRef(f"{base.rstrip('/')}/equip/{_slug(device_name)}-{digest}")


def _snapshot_point_node(base: str, device_ord: str, pt_ord: str, pt_name: str) -> URIRef:
    key = f"pt|{device_ord}|{pt_ord or pt_name}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return URIRef(f"{base.rstrip('/')}/point/{digest}")


# QUDT quantity kind local name, taken from the unit (not from the point's display name).
QUANTITY_KIND_FROM_UNIT: dict[str, str] = {
    "DEG_F": "Temperature", "DEG_C": "Temperature", "DEG_K": "Temperature",
    "IN_H2O": "Pressure", "PA": "Pressure", "KiloPA": "Pressure", "PSI": "Pressure",
    "PERCENT": "Dimensionless", "PERCENT_RH": "RelativeHumidity",
    "FT3-PER-MIN": "VolumeFlowRate", "M3-PER-SEC": "VolumeFlowRate",
    "GAL_US-PER-MIN": "VolumeFlowRate",
    "W": "Power", "KiloW": "Power", "KiloW-HR": "Energy",
    "HZ": "Frequency", "PPM": "DimensionlessRatio",
}
QK = Namespace("http://qudt.org/vocab/quantitykind/")
OWL = Namespace("http://www.w3.org/2002/07/owl#")
S223_ONTOLOGY = URIRef("http://data.ashrae.org/standard223/1.0/model/all")


def _s223_property_classes(value_kind: str, writable: bool) -> tuple[URIRef, ...]:
    """Concrete 223P property class plus every superclass the official NodeShapes target.

    Official 223P shapes are NodeShapes on each class. Without a reasoner, implicit
    ``sh:targetClass`` only applies when the instance carries that type, so the
    superclass chain is written out (``QuantifiableProperty``, ``EnumerableProperty``,
    ``Concept``, ...). Brick / 223P *equipment* subclasses are never inferred from names.
    """
    kind = (value_kind or "numeric").casefold()
    if kind in {"boolean", "enum"}:
        leaf = S223.EnumeratedActuatableProperty if writable else S223.EnumeratedObservableProperty
        role = S223.ActuatableProperty if writable else S223.ObservableProperty
        return (leaf, role, S223.EnumerableProperty, S223.Property, S223.Concept)
    leaf = S223.QuantifiableActuatableProperty if writable else S223.QuantifiableObservableProperty
    role = S223.ActuatableProperty if writable else S223.ObservableProperty
    return (leaf, role, S223.QuantifiableProperty, S223.Property, S223.Concept)


def map_quantity_kind(unit_local: str | None) -> URIRef | None:
    """QUDT QuantityKind IRI for a known unit. Returns None rather than guessing."""
    if not unit_local:
        return None
    name = QUANTITY_KIND_FROM_UNIT.get(unit_local)
    return QK[name] if name else None


def _enumeration_kind(graph: Graph, base: str, pt_iri: URIRef, value_kind: str, facets: dict) -> URIRef | None:
    """Attach ``s223:hasEnumerationKind``. Booleans use the 223P Binary kind; other enums get a local kind labelled from the facet range (not from the point name)."""
    kind = (value_kind or "").casefold()
    if kind == "boolean" or (facets.get("trueText") or facets.get("falseText")):
        ek = S223["EnumerationKind-Binary"]
        graph.add((ek, RDF.type, S223.EnumerationKind))
        graph.add((pt_iri, S223.hasEnumerationKind, ek))
        return ek
    if kind != "enum":
        return None
    range_val = facets.get("range") or facets.get("enum") or ""
    label = range_val if isinstance(range_val, str) else json.dumps(range_val, sort_keys=True)
    digest = hashlib.sha256(f"enum|{label}".encode("utf-8")).hexdigest()[:16]
    ek = URIRef(f"{base.rstrip('/')}/enumeration-kind/{digest}")
    graph.add((ek, RDF.type, S223.EnumerationKind))
    graph.add((ek, RDF.type, S223.Concept))
    if label:
        graph.add((ek, RDFS.label, Literal(str(label)[:200])))
    graph.add((pt_iri, S223.hasEnumerationKind, ek))
    return ek


def _infer_demo_bacnet_id(
    value_kind: str, writable: bool, point_name: str, counters: dict[str, int]
) -> tuple[str, int]:
    kind = (value_kind or "numeric").casefold()
    if kind == "numeric":
        if not writable:
            obj_type = "analog-input"
        elif "setpoint" in point_name.casefold() or point_name.casefold().endswith("sp"):
            obj_type = "analog-value"
        else:
            obj_type = "analog-output"
    elif kind == "boolean":
        obj_type = "binary-output" if writable else "binary-input"
    else:
        obj_type = "multi-state-value"
    counters[obj_type] = counters.get(obj_type, 0) + 1
    return obj_type, counters[obj_type]


def snapshot_from_points(points: list[BacnetPoint], station_label: str = "inventory") -> dict[str, Any]:
    """Wrap a CSV/scan-TTL BacnetPoint list into the normalized snapshot shape."""
    by_dev: dict[int, dict[str, Any]] = {}
    for pt in points:
        dev = by_dev.setdefault(
            pt.device_instance,
            {
                "ord": f"bacnet://{pt.device_instance}",
                "name": pt.device_name or f"Device_{pt.device_instance}",
                "typeSpec": "bacnet:BacnetDevice",
                "device_instance": pt.device_instance,
                "tags": [],
                "points": [],
            },
        )
        writable = pt.object_type in {"analog-output", "analog-value", "binary-output", "binary-value", "multi-state-output", "multi-state-value"}
        value_kind = "boolean" if "binary" in pt.object_type else ("enum" if "multi-state" in pt.object_type else "numeric")
        bac = {
            "object_type": pt.object_type,
            "object_instance": pt.object_instance,
            "object_identifier": pt.object_id,
            "property_identifier": pt.property_identifier,
        }
        if pt.property_array_index is not None:
            bac["property_array_index"] = pt.property_array_index
        if pt.priority_for_writing is not None:
            bac["priority_for_writing"] = pt.priority_for_writing
        dev["points"].append({
            "ord": pt.bacnet_uri,
            "name": pt.object_name,
            "display": pt.description or pt.object_name,
            "typeSpec": "",
            "valueKind": value_kind,
            "writable": writable,
            "facets": {},
            "haystackTags": [t for t in re.split(r"[\s,;]+", pt.haystack_tags) if t],
            "brick_class": pt.brick_class,
            "bacnet": bac,
        })
    return {
        "format": "baskstream-discovery-snapshot",
        "version": 1,
        "station": station_label,
        "base": "",
        "apiVersion": "",
        "module": "inventory",
        "devices": [by_dev[k] for k in sorted(by_dev)],
    }


def build_223p_graph(
    snapshot: dict[str, Any],
    base: str,
    *,
    mappings_path: Path | None = None,
    infer_demo_ids: bool = False,
) -> tuple[Graph, list[BacnetPoint], dict[str, Any]]:
    """Build an external ASHRAE 223P + Brick Turtle graph from a discovery snapshot."""
    if not valid_external_iri(base):
        raise ValueError("Base must be a fully qualified HTTP(S) or URN IRI")
    devices = snapshot.get("devices") or []
    if not devices:
        raise ValueError("Snapshot contains no devices")

    approved = load_approved_mappings(mappings_path) if mappings_path else {}

    graph = Graph()
    graph.bind("s223", S223)
    graph.bind("bacnet", BACNET)
    graph.bind("bacnet2024", BACNET_2024)
    graph.bind("ref", BRICK_REF)
    graph.bind("qudt", QUDT)
    graph.bind("unit", UNIT)
    graph.bind("oc", VOCAB)
    graph.bind("rdfs", RDFS)
    graph.bind("owl", OWL)
    graph.bind("qk", QK)

    ont = URIRef(base.rstrip("/") + "/")
    graph.add((ont, RDF.type, OWL.Ontology))
    graph.add((ont, OWL.imports, S223_ONTOLOGY))

    bacnet_points: list[BacnetPoint] = []
    seen_bacnet: dict[tuple[int, str, int], str] = {}
    unbound_points: list[dict[str, str]] = []
    units_mapped = 0
    units_unmapped: list[str] = []
    approved_applied = 0
    synthetic_count = 0

    for dev in devices:
        dev_ord = str(dev.get("ord") or "")
        dev_name = decode_n4_slot_text(str(dev.get("name") or dev_ord.rsplit("/", 1)[-1] or "Device"))
        raw_inst = dev.get("device_instance")
        dev_instance = int(raw_inst) if raw_inst not in (None, "") else None

        equip_iri = _equip_node(base, dev_ord, dev_name, dev_instance)
        graph.add((equip_iri, RDF.type, S223.Equipment))
        graph.add((equip_iri, RDF.type, S223.Connectable))
        graph.add((equip_iri, RDF.type, S223.Concept))
        graph.add((equip_iri, RDF.type, VOCAB.Equipment))
        graph.add((equip_iri, RDFS.label, Literal(dev_name)))
        if dev_ord:
            graph.add((equip_iri, VOCAB.niagaraOrd, Literal(dev_ord)))

        dev_bacnet_name = decode_n4_slot_text(str(dev.get("bacnet_device_name") or ""))
        if dev_instance is not None:
            add_bacnet_device(graph, dev_instance, dev_bacnet_name)

        demo_counters: dict[str, int] = {}
        for pt in dev.get("points") or []:
            pt_ord = str(pt.get("ord") or "")
            pt_name = decode_n4_slot_text(str(pt.get("name") or pt.get("display") or pt_ord.rsplit("/", 1)[-1]))
            pt_display = decode_n4_slot_text(str(pt.get("display") or pt_name))
            value_kind = str(pt.get("valueKind") or "numeric")
            writable = bool(pt.get("writable"))
            facets = pt.get("facets") or {}
            unit_raw = str(facets.get("units") or "").strip()

            pt_iri = _snapshot_point_node(base, dev_ord, pt_ord, pt_name)
            graph.add((equip_iri, S223.hasProperty, pt_iri))
            for s223_cls in _s223_property_classes(value_kind, writable):
                graph.add((pt_iri, RDF.type, s223_cls))
            graph.add((pt_iri, RDF.type, VOCAB.Point))
            if writable:
                graph.add((equip_iri, S223.actuatedByProperty, pt_iri))
            graph.add((pt_iri, RDFS.label, Literal(pt_display)))
            graph.add((pt_iri, VOCAB.object_name, Literal(pt_name)))
            if pt_ord:
                graph.add((pt_iri, VOCAB.niagaraOrd, Literal(pt_ord)))
            if pt.get("typeSpec"):
                graph.add((pt_iri, VOCAB.niagaraTypeSpec, Literal(str(pt["typeSpec"]))))

            qudt_unit = map_qudt_unit(unit_raw)
            if qudt_unit:
                unit_iri = UNIT[qudt_unit]
                graph.add((unit_iri, RDF.type, QUDT.Unit))
                graph.add((pt_iri, QUDT.hasUnit, unit_iri))
                qk = map_quantity_kind(qudt_unit)
                if qk is not None:
                    graph.add((qk, RDF.type, QUDT.QuantityKind))
                    graph.add((pt_iri, QUDT.hasQuantityKind, qk))
                units_mapped += 1
            elif unit_raw and unit_raw.lower() not in {"null", "none", "no-units", "nounits"}:
                graph.add((pt_iri, VOCAB.unitRaw, Literal(unit_raw)))
                units_unmapped.append(unit_raw)
            _enumeration_kind(graph, base, pt_iri, value_kind, facets if isinstance(facets, dict) else {})

            if facets:
                graph.add((pt_iri, VOCAB.facets, Literal(json.dumps(facets, sort_keys=True))))

            hs_tags = [str(t).strip() for t in (pt.get("haystackTags") or []) if str(t).strip()]
            if hs_tags:
                graph.add((pt_iri, VOCAB.haystackTags, Literal(" ".join(hs_tags))))

            # Approved Brick/223P class assignment only (from --mappings or verified input).
            mapped_class = str(pt.get("brick_class") or "").strip()
            for candidate_key in (pt_ord, pt_name, f"{dev_name}/{pt_name}"):
                if not mapped_class and candidate_key in approved:
                    mapped_class = approved[candidate_key]
            if mapped_class and valid_external_iri(mapped_class):
                graph.add((pt_iri, RDF.type, URIRef(mapped_class)))
                approved_applied += 1

            bac = dict(pt.get("bacnet") or {})
            obj_type = bac.get("object_type")
            obj_inst = bac.get("object_instance")
            if (not obj_type or obj_inst is None) and bac.get("object_identifier"):
                parsed = parse_bacnet_object_id(str(bac["object_identifier"]).replace(",", ":"))
                if parsed:
                    obj_type, obj_inst = parsed

            if (obj_type is None or obj_inst is None) and dev_instance is not None and infer_demo_ids:
                obj_type, obj_inst = _infer_demo_bacnet_id(value_kind, writable, pt_name, demo_counters)
                synthetic_count += 1

            if dev_instance is not None and obj_type is not None and obj_inst is not None:
                norm_type = normalize_object_type(str(obj_type))
                inst_num = int(obj_inst)
                addr = (dev_instance, norm_type, inst_num)
                if addr in seen_bacnet:
                    raise ValueError(
                        f"Duplicate BACnet object {norm_type},{inst_num} on device {dev_instance} "
                        f"({seen_bacnet[addr]} vs {pt_name})"
                    )
                seen_bacnet[addr] = pt_name
                prop_id = normalize_property_identifier(str(bac.get("property_identifier") or "present-value"))
                arr_idx = bac.get("property_array_index")
                write_pri = bac.get("priority_for_writing", pt.get("priority_for_writing"))

                bp = BacnetPoint(
                    device_instance=dev_instance,
                    object_type=norm_type,
                    object_instance=inst_num,
                    object_name=str(bac.get("object_name") or pt_name),
                    object_name_is_bacnet=bool(bac.get("object_name")),
                    device_name_is_bacnet=bool(dev_bacnet_name),
                    description=pt_display if pt_display != pt_name else "",
                    device_name=dev_bacnet_name or dev_name,
                    property_identifier=prop_id,
                    brick_class=mapped_class,
                    haystack_tags=" ".join(hs_tags),
                    property_array_index=int(arr_idx) if arr_idx not in (None, "") else None,
                    priority_for_writing=int(write_pri) if write_pri not in (None, "") else None,
                )
                bacnet_points.append(bp)

                add_bacnet_reference(graph, base, bp, pt_iri)
                graph.add((pt_iri, RDF.type, VOCAB.AddressedPoint))
            else:
                unbound_points.append({
                    "device": dev_name,
                    "ord": pt_ord,
                    "name": pt_name,
                    "reason": "missing BACnet device_instance or proxyExt objectId",
                })

    total_points = sum(len(d.get("points") or []) for d in devices)
    summary = {
        "devices": len(devices),
        "points": total_points,
        "bacnet_bound_points": len(bacnet_points),
        "unbound_points": len(unbound_points),
        "unbound_details": unbound_points[:50],
        "synthetic_bacnet_ids": synthetic_count,
        "qudt_units_mapped": units_mapped,
        "qudt_units_unmapped": sorted(set(units_unmapped)),
        "approved_classes_applied": approved_applied,
        "triples": len(graph),
    }
    return graph, bacnet_points, summary


def validate_223p_references(graph: Graph) -> dict[str, Any]:
    """Validate 223P BACnetExternalReference and Equipment/Property structure via rdflib."""
    violations: list[dict[str, str]] = []
    refs = set(graph.subjects(RDF.type, S223.BACnetExternalReference))
    seen_uris: dict[str, str] = {}

    single_valued = (
        (BACNET["object-identifier"], "bacnet:object-identifier"),
        (BACNET["device-identifier"], "bacnet:device-identifier"),
        (BACNET["object-name"], "bacnet:object-name"),
        (BACNET["device-name"], "bacnet:device-name"),
        (BACNET["property-identifier"], "bacnet:property-identifier"),
        (BACNET["priority-for-writing"], "bacnet:priority-for-writing"),
        (BRICK_REF.BACnetURI, "ref:BACnetURI"),
    )
    for ref in sorted(refs, key=str):
        # 223P 11.18.1: each of these is "at most one" per reference. Two values usually
        # means two sources (e.g. a scan and Niagara) disagree after a merge.
        for pred, label in single_valued:
            values = sorted({str(v) for v in graph.objects(ref, pred)})
            if len(values) > 1:
                violations.append({
                    "node": str(ref),
                    "path": label,
                    "message": f"more than one {label}: {values}",
                })
        oid = graph.value(ref, BACNET["object-identifier"])
        if oid is None or not IDENT_PATTERN.fullmatch(str(oid)):
            violations.append({
                "node": str(ref),
                "path": "bacnet:object-identifier",
                "message": f"Invalid or missing 223P object-identifier (expected type,instance>=1): {oid!r}",
            })
        did = graph.value(ref, BACNET["device-identifier"])
        if did is not None and not IDENT_PATTERN.fullmatch(str(did)):
            violations.append({
                "node": str(ref),
                "path": "bacnet:device-identifier",
                "message": f"Invalid 223P device-identifier pattern: {did!r}",
            })
        uri = graph.value(ref, BRICK_REF.BACnetURI)
        if uri is None or not str(uri).startswith("bacnet://"):
            violations.append({
                "node": str(ref),
                "path": "ref:BACnetURI",
                "message": f"Missing or invalid Clause Q.8 BACnetURI: {uri!r}",
            })
        else:
            u = str(uri)
            if u in seen_uris:
                violations.append({
                    "node": str(ref),
                    "path": "ref:BACnetURI",
                    "message": f"Duplicate BACnetURI {u} (already bound to {seen_uris[u]})",
                })
            seen_uris[u] = str(ref)
        pri = graph.value(ref, BACNET["priority-for-writing"])
        if pri is not None:
            try:
                val = int(str(pri))
                if not (1 <= val <= 16):
                    raise ValueError()
            except ValueError:
                violations.append({
                    "node": str(ref),
                    "path": "bacnet:priority-for-writing",
                    "message": f"priority-for-writing must be 1..16: {pri!r}",
                })
        idx = graph.value(ref, BACNET["property-array-index"])
        if idx is not None:
            try:
                if int(str(idx)) < 0:
                    raise ValueError()
            except ValueError:
                violations.append({
                    "node": str(ref),
                    "path": "bacnet:property-array-index",
                    "message": f"property-array-index must be non-negative integer: {idx!r}",
                })
        if not list(graph.subjects(S223.hasExternalReference, ref)):
            violations.append({
                "node": str(ref),
                "path": "^s223:hasExternalReference",
                "message": "Orphan BACnetExternalReference not linked from any s223:Property",
            })

    equips = set(graph.subjects(RDF.type, S223.Equipment))
    for eq in sorted(equips, key=str):
        if not list(graph.objects(eq, S223.hasProperty)):
            violations.append({
                "node": str(eq),
                "path": "s223:hasProperty",
                "message": "s223:Equipment has no s223:hasProperty links",
            })

    return {
        "conforms": len(violations) == 0,
        "engine": "rdflib-223p-ref-check",
        "bacnet_external_references": len(refs),
        "equipments": len(equips),
        "violations": violations,
    }


def coverage_223p(
    snapshot: dict[str, Any],
    catalogs: dict[str, Path] | None = None,
    *,
    cutoff: float = 0.86,
) -> dict[str, Any]:
    """Summarize 223P / Brick / QUDT / Haystack coverage and optional candidate term matches."""
    loaded_catalogs: dict[str, dict] = {}
    if catalogs:
        for name, path in catalogs.items():
            if not path.is_file():
                raise FileNotFoundError(f"Missing ontology catalog: {path}")
            loaded_catalogs[name] = label_index(Graph().parse(str(path), format="turtle"))

    point_rows: list[dict[str, Any]] = []
    candidates_found = 0
    for dev in snapshot.get("devices") or []:
        for pt in dev.get("points") or []:
            name = decode_n4_slot_text(str(pt.get("display") or pt.get("name") or ""))
            row: dict[str, Any] = {
                "device": dev.get("name"),
                "name": name,
                "s223_property_types": [str(u).split("#")[-1] for u in _s223_property_classes(str(pt.get("valueKind") or "numeric"), bool(pt.get("writable")))],
                "has_bacnet_id": bool((pt.get("bacnet") or {}).get("object_identifier")),
                "qudt_unit": map_qudt_unit(str((pt.get("facets") or {}).get("units") or "")),
                "haystack_tags": pt.get("haystackTags") or [],
            }
            if loaded_catalogs:
                row["catalog_candidates_REVIEW"] = {}
                for cname, idx in loaded_catalogs.items():
                    matches = find_matches(name, cname, idx, top=2, cutoff=cutoff)
                    if matches:
                        candidates_found += 1
                    row["catalog_candidates_REVIEW"][cname] = [
                        {"label": m.label, "iri": m.uri, "score": m.score, "status": m.status}
                        for m in matches
                    ]
            point_rows.append(row)

    return {
        "points_analyzed": len(point_rows),
        "catalogs": list(loaded_catalogs.keys()),
        "points_with_lexical_candidates": candidates_found,
        "note": "Lexical catalog candidates are REVIEW hints only; never auto-assigned as rdf:type.",
        "rows": point_rows,
    }



def validate_official_223p(
    graph: Graph,
    shapes_path: Path,
    *,
    inference: str = "none",
) -> dict[str, Any]:
    """Validate ``graph`` against official 223P SHACL shapes (``223p.ttl``).

    Prefers Shifty when the ``pyshifty`` package is installed (``import shifty``),
    otherwise falls back to pyshacl. The shapes file is the 223P ontology + SHACL
    shapes (for example https://open223.info/223p.ttl). OntoEnv is the way to
    resolve that file's ``owl:imports`` (QUDT, etc.) into a closure first; pass
    the resulting graph serialized as ``shapes_path``, or the 223p.ttl file alone
    when you only need the 223P shapes.

    Returns a JSON-serializable report. ``conforms`` is True only when there are
    no SHACL Violations. Warnings (for example a missing ``s223:Sensor`` that
    ``observes`` a property) are recorded but do not fail conformance: this
    exporter writes an external-reference graph, not a full 223 topology.
    """
    shapes_path = Path(shapes_path)
    if not shapes_path.is_file():
        raise ValueError(f"223P shapes file not found: {shapes_path}")

    violations: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    engine = "unknown"
    text = ""
    conforms = False

    try:
        import shifty  # type: ignore
        engine = "shifty"
        result = shifty.validate_algebra(graph, str(shapes_path))
        conforms = bool(result.conforms)
        text = str(getattr(result, "text", "") or "")
        for item in getattr(result, "violations", []) or []:
            severity = str(getattr(item, "severity", "Violation") or "Violation")
            row = {
                "severity": severity,
                "focus": str(getattr(item, "focus_node", "")),
                "message": str(item),
            }
            (warnings if severity.lower() == "warning" else violations).append(row)
    except ImportError:
        from pyshacl import validate as pyshacl_validate
        engine = "pyshacl"
        # 223P SPARQL constraints look up `sh:property` on s223 classes, which live
        # in the ontology. Union the ontology with the model for validation only
        # (the published TTL stays a data graph). Then keep findings whose focus
        # node is in the model, so ontology-internal nodes are not reported.
        model_nodes = {str(s) for s in graph.subjects()}
        union = Graph()
        union += graph
        union.parse(str(shapes_path), format="turtle")
        conforms_raw, report, text = pyshacl_validate(
            data_graph=union,
            shacl_graph=str(shapes_path),
            inference=inference,
            abort_on_first=False,
            allow_infos=True,
            allow_warnings=True,
        )
        from rdflib.namespace import SH as _SH
        for res in report.subjects(RDF.type, _SH.ValidationResult):
            focus = str(report.value(res, _SH.focusNode) or "")
            if focus not in model_nodes:
                continue
            sev = str(report.value(res, _SH.resultSeverity) or "").rsplit("#", 1)[-1]
            row = {
                "severity": sev,
                "focus": focus,
                "path": str(report.value(res, _SH.resultPath) or ""),
                "message": str(report.value(res, _SH.resultMessage) or ""),
            }
            if sev.lower() == "warning":
                warnings.append(row)
            elif sev.lower() != "info":
                violations.append(row)
        conforms = len(violations) == 0
        _ = conforms_raw

    return {
        "conforms": conforms,
        "engine": engine,
        "shapes": str(shapes_path),
        "violations": violations,
        "warnings": warnings,
        "violation_count": len(violations),
        "warning_count": len(warnings),
        "report_text": text[:20000],
    }

def export_223p(
    *,
    base: str,
    out: Path,
    snapshot_path: Path | None = None,
    input_path: Path | None = None,
    station: str | None = None,
    slot_base: str = "slot:/Drivers",
    env_file: Path | None = None,
    insecure: bool = False,
    mappings_path: Path | None = None,
    catalogs: dict[str, Path] | None = None,
    manifest: Path | None = None,
    validate_out: Path | None = None,
    infer_demo_ids: bool = False,
    ws_auth_header: bool = False,
    discovery_limit: int = 1000,
    save_snapshot: Path | None = None,
    official_shapes: Path | None = None,
) -> dict[str, Any]:
    """Run the full read-only discovery -> external 223P TTL -> validate/coverage pipeline.

    ``save_snapshot`` writes the (credential-free) live discovery snapshot so later runs
    can use ``--snapshot`` offline without touching the station again.
    """
    sources = [bool(snapshot_path), bool(input_path), bool(station)]
    if sum(sources) != 1:
        raise ValueError("Provide exactly one of --snapshot, --input, or --station")

    if snapshot_path is not None:
        snapshot = load_baskstream_snapshot(snapshot_path)
    elif input_path is not None:
        pts = load_points(input_path)
        snapshot = snapshot_from_points(pts, station_label=input_path.name)
    else:
        assert station is not None
        snapshot = discover_station_snapshot(
            station,
            base=slot_base,
            env_file=env_file,
            insecure=insecure,
            ws_auth_header=ws_auth_header,
            limit=discovery_limit,
        )
        if save_snapshot is not None:
            save_snapshot.parent.mkdir(parents=True, exist_ok=True)
            save_snapshot.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    graph, bacnet_points, summary = build_223p_graph(
        snapshot,
        base,
        mappings_path=mappings_path,
        infer_demo_ids=infer_demo_ids,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    graph.serialize(destination=str(out), format="turtle")
    summary["output"] = str(out)
    summary["source"] = str(snapshot_path or input_path or station)
    summary["api_version"] = str(snapshot.get("apiVersion") or "")
    summary["warnings"] = list(snapshot.get("warnings") or [])
    if save_snapshot is not None and station is not None:
        summary["snapshot_saved"] = str(save_snapshot)

    if manifest is not None and bacnet_points:
        summary.update(write_manifest(bacnet_points, base, manifest))
        # Also confirm round-trip parseability of bindings from the emitted TTL.
        summary["manifest_roundtrip_rows"] = len(bindings_from_graph(graph))

    validation = validate_223p_references(graph)
    official = None
    if official_shapes is not None:
        official = validate_official_223p(graph, official_shapes)
        summary["official_223p"] = {k: v for k, v in official.items() if k != "report_text"}
        summary["official_223p"]["report_text"] = official.get("report_text", "")
    coverage = coverage_223p(snapshot, catalogs=catalogs)
    summary["validation"] = validation
    summary["coverage"] = {
        "points_analyzed": coverage["points_analyzed"],
        "catalogs": coverage["catalogs"],
        "points_with_lexical_candidates": coverage["points_with_lexical_candidates"],
    }

    if validate_out is not None:
        validate_out.mkdir(parents=True, exist_ok=True)
        (validate_out / "validation-223p.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
        (validate_out / "coverage-223p.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
        if official is not None:
            (validate_out / "official-223p-shacl.json").write_text(json.dumps(official, indent=2), encoding="utf-8")
        if station is not None:
            (validate_out / "baskstream-snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        summary["validate_dir"] = str(validate_out)

    return summary
