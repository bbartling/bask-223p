"""Reconcile a Niagara/baskStream export with a direct BACnet scan.

Joins the two sides on the BACnet identity ``(device instance, object type,
object instance)`` -- the same key both TTLs carry in their shared reference
shape (``bacnet://<device>/<type>,<instance>``, see :func:`refs.add_bacnet_reference`)
-- and reports, per object:

* ``matched``             present on both sides,
* ``missing_in_niagara``  the scan found it, Niagara has no proxy point for it,
* ``missing_in_scan``     Niagara points at an object the scan did not find.

Matched rows also flag name and unit differences (Niagara display name vs the
BACnet ``object-name``; Niagara facets vs BACnet ``units``) for a person to review.
Nothing is written to any station or device; inputs are files.

Accepted inputs
  Niagara side: an ``export-223p`` TTL, or a baskStream discovery snapshot JSON.
  Scan side:    a BACpypes3 BACnet-to-RDF site-scan TTL (bacnet/2020 or /2024),
                or its CSV (``DeviceID,ObjType,ObjInst,Name,...,Units``),
                or any TTL/CSV ``export-bacnet-refs`` accepts.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, RDFS

from .baskstream import load_baskstream_snapshot, parse_bacnet_object_id
from .units import BACNET_UNIT_QUDT, map_qudt_unit, resolve_qudt_unit
from .refs import (
    BACNET,
    BACNET_2024,
    NIAGARA,
    S223,
    VOCAB,
    BacnetPoint,
    bacnet_object_uri,
    build_reference_graph,
    load_points,
    normalize_object_type,
)

QUDT_HAS_UNIT = URIRef("http://qudt.org/schema/qudt/hasUnit")

# Object types that carry point data. Others (file, program, schedule, calendar,
# trend-log, notification-class, ...) are counted but not reported as missing.
POINT_TYPES = frozenset({
    "analog-input", "analog-output", "analog-value",
    "binary-input", "binary-output", "binary-value",
    "multi-state-input", "multi-state-output", "multi-state-value",
    "integer-value", "positive-integer-value", "large-analog-value",
    "characterstring-value", "accumulator", "pulse-converter", "loop",
    "binary-lighting-output", "lighting-output",
})

@dataclass
class Side:
    """One object as seen by one side."""
    device_instance: int
    object_type: str
    object_instance: int
    name: str = ""
    name_is_bacnet: bool = False
    unit: str | None = None       # QUDT local name when known
    unit_raw: str = ""
    ord: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[int, str, int]:
        return (self.device_instance, self.object_type, self.object_instance)


def _norm_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").casefold())


def _unit_from_bacnet(raw: str) -> str | None:
    return resolve_qudt_unit(raw)


# ---------------------------------------------------------------- loaders

def _sides_from_ref_graph(graph: Graph, *, niagara_graph: Graph | None = None) -> list[Side]:
    """Objects from a graph that carries 223P/Brick external references (export-223p)."""
    out: list[Side] = []
    for ref in set(graph.subjects(RDF.type, S223.BACnetExternalReference)):
        obj_id = str(graph.value(ref, BACNET["object-identifier"]) or "")
        dev_id = str(graph.value(ref, BACNET["device-identifier"]) or "")
        uri = str(graph.value(ref, URIRef("https://brickschema.org/schema/Brick/ref#BACnetURI")) or "")
        m = re.match(r"bacnet://(\d+)/", uri)
        if dev_id.startswith("device,"):
            dev = int(dev_id.split(",", 1)[1])
        elif m:
            dev = int(m.group(1))
        else:
            continue
        if "," not in obj_id:
            continue
        typ, inst = obj_id.split(",", 1)
        holder = graph.value(None, S223.hasExternalReference, ref)
        bacnet_name = str(graph.value(ref, BACNET["object-name"]) or "")
        name = bacnet_name
        if not name and holder is not None:
            name = str(graph.value(holder, VOCAB.object_name) or graph.value(holder, RDFS.label) or "")
            if not name and niagara_graph is not None:
                name = str(
                    niagara_graph.value(holder, NIAGARA.pointName)
                    or niagara_graph.value(holder, NIAGARA.displayName)
                    or ""
                )
        unit = None
        if holder is not None:
            u = graph.value(holder, QUDT_HAS_UNIT)
            unit = str(u).rsplit("/", 1)[-1] if u is not None else None
        ord_value = ""
        if holder is not None:
            ord_value = str(graph.value(holder, VOCAB.niagaraOrd) or "")
            if not ord_value and niagara_graph is not None:
                ord_value = str(
                    niagara_graph.value(holder, NIAGARA.slotPath)
                    or niagara_graph.value(holder, VOCAB.niagaraOrd)
                    or ""
                )
        dev_name = str(graph.value(ref, BACNET["device-name"]) or "")
        out.append(Side(dev, normalize_object_type(typ), int(inst), name, bool(bacnet_name), unit, "", ord_value,
                        {"device_name": dev_name}))
    return out


def _sides_from_snapshot(path: Path) -> list[Side]:
    snap = load_baskstream_snapshot(path)
    out: list[Side] = []
    for dev in snap.get("devices") or []:
        inst = dev.get("device_instance")
        if inst is None:
            continue
        for pt in dev.get("points") or []:
            bac = pt.get("bacnet") or {}
            typ, oi = bac.get("object_type"), bac.get("object_instance")
            if (typ is None or oi is None) and bac.get("object_identifier"):
                parsed = parse_bacnet_object_id(str(bac["object_identifier"]).replace(",", ":"))
                if parsed:
                    typ, oi = parsed
            if typ is None or oi is None:
                continue
            units = str((pt.get("facets") or {}).get("units") or "")
            out.append(Side(int(inst), normalize_object_type(str(typ)), int(oi),
                            str(bac.get("object_name") or pt.get("name") or ""), bool(bac.get("object_name")),
                            map_qudt_unit(units), units, str(pt.get("ord") or ""),
                            {"device_name": str(dev.get("bacnet_device_name") or "")}))
    return out


def _scan_units_from_ttl(graph: Graph) -> dict[str, str]:
    units: dict[str, str] = {}
    for ns in (BACNET, BACNET_2024):
        for subj, val in graph.subject_objects(ns["units"]):
            units[str(subj)] = str(val)
    return units


def _sides_from_scan(path: Path) -> tuple[list[Side], list[BacnetPoint]]:
    points: list[BacnetPoint] = load_points(path)
    out = []
    for p in points:
        raw = p.unit_raw
        out.append(Side(p.device_instance, p.object_type, p.object_instance, p.object_name, True,
                        p.qudt_unit or None, raw.rsplit("EngineeringUnits.", 1)[-1] if raw else "",
                        "", {"description": p.description, "device_name": p.device_name}))
    return out, points


def load_niagara_side(path: Path) -> tuple[list[Side], Graph | None]:
    if path.suffix.lower() == ".json":
        return _sides_from_snapshot(path), None
    graph = Graph().parse(path, format="turtle")
    sidecar = path.with_suffix(".niagara.ttl")
    niagara_graph = Graph().parse(sidecar, format="turtle") if sidecar.is_file() else None
    return _sides_from_ref_graph(graph, niagara_graph=niagara_graph), graph


# ---------------------------------------------------------------- reconcile

def reconcile(niagara: list[Side], scan: list[Side], *, all_object_types: bool = False) -> dict[str, Any]:
    n_index = {s.key: s for s in niagara}
    ignored: dict[str, int] = {}
    s_index: dict[tuple[int, str, int], Side] = {}
    for s in scan:
        if s.object_type in ("device", "network-port"):
            continue
        if not all_object_types and s.object_type not in POINT_TYPES and s.key not in n_index:
            ignored[s.object_type] = ignored.get(s.object_type, 0) + 1
            continue
        s_index[s.key] = s

    rows: list[dict[str, Any]] = []
    for key in sorted(set(n_index) | set(s_index)):
        n, s = n_index.get(key), s_index.get(key)
        status = "matched" if n and s else ("missing_in_niagara" if s else "missing_in_scan")
        row: dict[str, Any] = {
            "status": status,
            "device_instance": key[0],
            "object_identifier": f"{key[1]},{key[2]}",
            "bacnet_uri": bacnet_object_uri(*key),
            "niagara_name": n.name if n else "",
            "niagara_ord": n.ord if n else "",
            "bacnet_object_name": s.name if s else "",
            "niagara_unit": (n.unit or n.unit_raw) if n else "",
            "bacnet_unit": (s.unit or s.unit_raw) if s else "",
            "name_differs": False,
            "unit_differs": False,
        }
        if n and s:
            row["name_differs"] = bool(n.name and s.name and _norm_name(n.name) != _norm_name(s.name))
            row["unit_differs"] = bool(n.unit and s.unit and n.unit != s.unit)
        rows.append(row)

    n_dev = {k[0] for k in n_index}
    s_dev = {k[0] for k in s_index}
    by_device = {}
    for dev in sorted(n_dev | s_dev):
        dev_rows = [r for r in rows if r["device_instance"] == dev]
        by_device[str(dev)] = {
            st: sum(1 for r in dev_rows if r["status"] == st)
            for st in ("matched", "missing_in_niagara", "missing_in_scan")
        }
    def _dev_names(sides) -> dict[int, str]:
        names: dict[int, str] = {}
        for side in sides:
            nm = str(side.extra.get("device_name") or "")
            if nm:
                names.setdefault(side.device_instance, nm)
        return names

    n_names, s_names = _dev_names(n_index.values()), _dev_names(scan)
    devices = []
    for dev in sorted(n_dev | s_dev):
        a, b = n_names.get(dev, ""), s_names.get(dev, "")
        devices.append({
            "device_instance": dev,
            "in_niagara": dev in n_dev,
            "in_scan": dev in s_dev,
            "niagara_bacnet_device_name": a,
            "scan_device_name": b,
            "device_name_differs": bool(a and b and a != b),
            **by_device[str(dev)],
        })
    counts = {st: sum(1 for r in rows if r["status"] == st)
              for st in ("matched", "missing_in_niagara", "missing_in_scan")}
    return {
        "join_key": "(device instance, object type, object instance) == bacnet://<device>/<type>,<instance>",
        **counts,
        "name_differs": sum(1 for r in rows if r["name_differs"]),
        "unit_differs": sum(1 for r in rows if r["unit_differs"]),
        "devices_only_in_niagara": sorted(n_dev - s_dev),
        "devices_only_in_scan": sorted(s_dev - n_dev),
        "by_device": by_device,
        "devices": devices,
        "device_name_differs": sum(1 for d in devices if d["device_name_differs"]),
        "ignored_scan_object_types": ignored,
        "rows": rows,
    }


def _base_from_graph(graph: Graph) -> str | None:
    for ref in graph.subjects(RDF.type, S223.BACnetExternalReference):
        text = str(ref)
        if "/bacnet-ref/" in text:
            return text.split("/bacnet-ref/", 1)[0]
    return None


def run_reconcile(
    niagara_path: Path,
    scan_path: Path,
    out: Path,
    *,
    base: str | None = None,
    merged_out: Path | None = None,
    all_object_types: bool = False,
) -> dict[str, Any]:
    """Reconcile two files and write ``reconcile.json`` / ``reconcile.csv`` under ``out``.

    With ``merged_out``, also writes the union of the Niagara TTL and the scan's
    reference graph (built with the same base, so shared objects share one
    reference node) and checks it with the 223P reference validator.
    """
    niagara, n_graph = load_niagara_side(niagara_path)
    scan, scan_points = _sides_from_scan(scan_path)
    result = reconcile(niagara, scan, all_object_types=all_object_types)
    result["niagara_source"] = niagara_path.name
    result["scan_source"] = scan_path.name

    out.mkdir(parents=True, exist_ok=True)
    rows = result["rows"]
    with (out / "reconcile.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = list(rows[0].keys()) if rows else ["status"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    (out / "reconcile.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    if merged_out is not None:
        from .export223 import validate_223p_references

        base = base or (n_graph is not None and _base_from_graph(n_graph)) or None
        if not base:
            raise ValueError("--merged-out needs --base (or a Niagara TTL whose refs carry the base)")
        keep = [p for p in scan_points if p.object_type not in ("device", "network-port")
                and (all_object_types or p.object_type in POINT_TYPES)]
        merged, _ = build_reference_graph(keep, base) if keep else (Graph(), {})
        if n_graph is not None:
            merged += n_graph
        merged_out.parent.mkdir(parents=True, exist_ok=True)
        merged.serialize(destination=str(merged_out), format="turtle")
        validation = validate_223p_references(merged)
        result["merged"] = {"path": str(merged_out), "triples": len(merged), "validation": validation}
        (out / "reconcile.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    result_summary = {k: v for k, v in result.items() if k != "rows"}
    result_summary["out"] = str(out)
    return result_summary


__all__ = ["POINT_TYPES", "Side", "load_niagara_side", "reconcile", "run_reconcile"]
