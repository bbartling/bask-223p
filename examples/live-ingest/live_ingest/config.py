"""Load point targets and optional RDF graph from 223P TTL, BACpypes3 scan TTL, CSV, or YAML."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rdflib import Graph, URIRef
from rdflib.namespace import RDF, RDFS

from bask_223p.bacnet.export223 import build_223p_graph, snapshot_from_points
from bask_223p.bacnet.reconcile import _sides_from_ref_graph, _sides_from_snapshot
from bask_223p.bacnet.refs import (
    BACNET,
    BRICK_REF,
    NIAGARA,
    S223,
    VOCAB,
    BacnetPoint,
    load_points,
    load_points_scan,
    normalize_object_type,
    normalize_property_identifier,
)

from .models import PointTarget

QUDT_HAS_UNIT = URIRef("http://qudt.org/schema/qudt/hasUnit")


@dataclass(slots=True)
class LoadedConfig:
    site: str = "demo-site"
    source: str | None = None
    station: str | None = None
    base_ord: str = "slot:/Drivers/BacnetNetwork"
    local_address: str = "0.0.0.0"
    max_props: int = 25
    interval_s: float = 15.0
    duckdb_path: str | None = None
    csv_path: str | None = None
    targets: list[PointTarget] = field(default_factory=list)
    graph: Graph | None = None
    niagara_graph: Graph | None = None
    scan_graph: Graph | None = None

    def sidecar_graphs(self) -> dict[str, Graph]:
        out: dict[str, Graph] = {}
        if self.niagara_graph is not None and len(self.niagara_graph) > 0:
            out["urn:bask-223p:niagara"] = self.niagara_graph
        if self.scan_graph is not None and len(self.scan_graph) > 0:
            out["urn:bask-223p:bacnet-scan"] = self.scan_graph
        return out


def _targets_from_ref_graph(
    graph: Graph,
    site: str,
    *,
    niagara_graph: Graph | None = None,
) -> list[PointTarget]:
    """Load PointTarget objects from a 223P/Brick external-reference TTL graph."""
    sides = _sides_from_ref_graph(graph, niagara_graph=niagara_graph)
    targets: list[PointTarget] = []
    ref_by_key: dict[tuple[int, str, int], tuple[str, str, str]] = {}
    for ref in set(graph.subjects(RDF.type, S223.BACnetExternalReference)) | set(
        graph.subjects(RDF.type, BRICK_REF.BACnetReference)
    ):
        obj_id = str(graph.value(ref, BACNET["object-identifier"]) or "")
        dev_id = str(graph.value(ref, BACNET["device-identifier"]) or "")
        uri = str(graph.value(ref, BRICK_REF.BACnetURI) or "")
        prop_id = normalize_property_identifier(str(graph.value(ref, BACNET["property-identifier"]) or "present-value"))
        dev_num: int | None = None
        if dev_id.startswith("device,") and dev_id.split(",", 1)[1].isdigit():
            dev_num = int(dev_id.split(",", 1)[1])
        elif uri.startswith("bacnet://"):
            part = uri[len("bacnet://"):].split("/", 1)[0]
            if part.isdigit():
                dev_num = int(part)
        if dev_num is None or "," not in obj_id:
            continue
        typ, inst = obj_id.split(",", 1)
        if not inst.isdigit():
            continue
        holder = graph.value(None, S223.hasExternalReference, ref) or graph.value(
            None, BRICK_REF.hasExternalReference, ref
        )
        key = (dev_num, normalize_object_type(typ), int(inst))
        eff_uri = uri or f"bacnet://{dev_num}/{key[1]},{key[2]}"
        ref_by_key[key] = (prop_id, str(holder or ""), eff_uri)

    for s in sides:
        prop_id, holder_uri, eff_uri = ref_by_key.get(
            s.key, ("present-value", "", f"bacnet://{s.device_instance}/{s.object_type},{s.object_instance}")
        )
        targets.append(
            PointTarget(
                site=site,
                device=str(s.device_instance),
                object_id=f"{s.object_type},{s.object_instance}",
                property_id=prop_id,
                units=s.unit or s.unit_raw or "",
                label=s.name,
                ord_path=s.ord,
                point_uri=holder_uri,
                ref=eff_uri,
            )
        )
    if targets:
        return targets

    for key, (prop_id, holder_uri, eff_uri) in sorted(ref_by_key.items()):
        dev_num, typ, inst = key
        label = ""
        unit = ""
        if holder_uri:
            h_ref = URIRef(holder_uri)
            label = str(graph.value(h_ref, VOCAB.object_name) or graph.value(h_ref, RDFS.label) or "")
            if not label and niagara_graph is not None:
                label = str(
                    niagara_graph.value(h_ref, NIAGARA.pointName)
                    or niagara_graph.value(h_ref, NIAGARA.displayName)
                    or ""
                )
            u = graph.value(h_ref, QUDT_HAS_UNIT)
            if u is not None:
                unit = str(u).rsplit("/", 1)[-1]
        targets.append(
            PointTarget(
                site=site,
                device=str(dev_num),
                object_id=f"{typ},{inst}",
                property_id=prop_id,
                units=unit,
                label=label,
                point_uri=holder_uri,
                ref=eff_uri,
            )
        )
    return targets


def _targets_from_bacnet_points(pts: list[BacnetPoint], site: str) -> list[PointTarget]:
    targets: list[PointTarget] = []
    for p in pts:
        if p.object_type in ("device", "network-port"):
            continue
        unit = p.qudt_unit or (
            p.unit_raw.rsplit("EngineeringUnits.", 1)[-1].rsplit("#", 1)[-1] if p.unit_raw else ""
        )
        targets.append(
            PointTarget(
                site=site,
                device=str(p.device_instance),
                object_id=p.object_id,
                property_id=p.property_identifier or "present-value",
                units=unit,
                label=p.object_name or p.description,
                point_uri=p.object_iri,
                ref=p.bacnet_uri,
            )
        )
    return targets


def _targets_from_csv(path: Path, site: str) -> list[PointTarget]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    if not rows:
        return []
    cols = {c.strip().casefold() for c in rows[0] if c}
    if "object" in cols and "device" in cols:
        targets: list[PointTarget] = []
        for row in rows:
            low = {k.strip().casefold(): (v or "").strip() for k, v in row.items() if k}
            obj = low.get("object", "")
            dev = low.get("device", "")
            if not obj or not dev or "," not in obj:
                continue
            typ, inst = obj.split(",", 1)
            targets.append(
                PointTarget(
                    site=low.get("site") or site,
                    device=dev,
                    object_id=f"{normalize_object_type(typ)},{inst.strip()}",
                    property_id=normalize_property_identifier(low.get("property") or "present-value"),
                    units=low.get("units", ""),
                    label=low.get("label") or low.get("name") or "",
                    ord_path=low.get("ord") or low.get("niagara_ord") or "",
                    device_address=low.get("device_address") or low.get("address") or "",
                )
            )
        return targets

    return _targets_from_bacnet_points(load_points(path), site)


def _targets_from_yaml(path: Path, site: str) -> LoadedConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a YAML mapping")
    eff_site = str(raw.get("site") or site)
    bs = raw.get("baskstream") or {}
    bn = raw.get("bacnet") or {}
    ddb = raw.get("duckdb") or {}
    csv_cfg = raw.get("csv") or {}
    cfg = LoadedConfig(
        site=eff_site,
        source=raw.get("source"),
        station=bs.get("station") or raw.get("station"),
        base_ord=str(bs.get("base") or raw.get("base") or "slot:/Drivers/BacnetNetwork"),
        local_address=str(bn.get("local_address") or "0.0.0.0"),
        max_props=int(bn.get("max_props") or 25),
        interval_s=float(raw.get("interval_s") or 15.0),
        duckdb_path=str(ddb.get("path")) if ddb.get("path") else None,
        csv_path=str(csv_cfg.get("path")) if csv_cfg.get("path") else None,
    )
    ttl_ref = raw.get("ttl") or raw.get("points_file")
    if ttl_ref:
        ref_path = (path.parent / str(ttl_ref)).resolve()
        sub = load_config(ref_path, site=eff_site)
        cfg.targets.extend(sub.targets)
        cfg.graph = sub.graph
        cfg.niagara_graph = sub.niagara_graph
        cfg.scan_graph = sub.scan_graph
    for item in raw.get("points") or []:
        if not isinstance(item, dict):
            continue
        obj = str(item.get("object") or item.get("object_id") or "")
        if "," in obj:
            typ, inst = obj.split(",", 1)
            obj = f"{normalize_object_type(typ)},{inst.strip()}"
        cfg.targets.append(
            PointTarget(
                site=str(item.get("site") or eff_site),
                device=str(item.get("device") or ""),
                object_id=obj,
                property_id=normalize_property_identifier(str(item.get("property") or "present-value")),
                units=str(item.get("units") or ""),
                label=str(item.get("label") or item.get("name") or ""),
                ord_path=str(item.get("ord") or ""),
                device_address=str(item.get("device_address") or item.get("address") or ""),
            )
        )
    return cfg


def load_config(path: str | Path, *, site: str = "demo-site", base_iri: str = "https://example.org/site") -> LoadedConfig:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    suffix = p.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return _targets_from_yaml(p, site)
    if suffix in {".ttl", ".turtle"}:
        raw_graph = Graph().parse(p, format="turtle")
        niagara_sidecar = p.with_suffix(".niagara.ttl")
        niagara_graph = Graph().parse(niagara_sidecar, format="turtle") if niagara_sidecar.is_file() else None
        scan_sidecar = p.with_suffix(".scan-sidecar.ttl")
        scan_graph = Graph().parse(scan_sidecar, format="turtle") if scan_sidecar.is_file() else None

        targets = _targets_from_ref_graph(raw_graph, site, niagara_graph=niagara_graph)
        if targets:
            return LoadedConfig(
                site=site,
                targets=targets,
                graph=raw_graph,
                niagara_graph=niagara_graph,
                scan_graph=scan_graph,
            )

        # BACpypes3 scan TTL: parse once -> load_points_scan -> snapshot_from_points -> single 223P emitter;
        # keep the raw scan TTL as a sidecar graph rather than mixing it into the 223P model.
        pts = load_points_scan(p, graph=raw_graph)
        snap = snapshot_from_points(pts, station_label=p.name)
        model_graph, _, _ = build_223p_graph(snap, base_iri)
        targets = _targets_from_bacnet_points(pts, site)
        return LoadedConfig(
            site=site,
            targets=targets,
            graph=model_graph,
            scan_graph=raw_graph,
        )
    if suffix == ".json":
        sides = _sides_from_snapshot(p)
        targets = [
            PointTarget(
                site=site,
                device=str(s.device_instance),
                object_id=f"{s.object_type},{s.object_instance}",
                property_id="present-value",
                units=s.unit or s.unit_raw or "",
                label=s.name,
                ord_path=s.ord,
            )
            for s in sides
        ]
        return LoadedConfig(site=site, targets=targets)
    if suffix == ".csv":
        return LoadedConfig(site=site, targets=_targets_from_csv(p, site))
    raise ValueError(f"Unsupported points/config format: {p.suffix}")
