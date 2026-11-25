"""Export BACnet device instance + object identifier as Brick and 223P references.

Bindings follow Brick ``ref:BACnetReference`` (bacnet/2020 ``object-identifier``,
``object-name``, ``objectOf`` a device, plus optional ``ref:BACnetURI``) and the
ASHRAE 223P ``BACnetExternalReference`` (``object-identifier``, ``object-name``,
``device-identifier``, ``device-name``, ``property-identifier`` defaulting to
``present-value``, and optionally ``property-array-index`` and
``priority-for-writing`` (ASHRAE 223P 2nd public review draft, section 11.18.1).

Device and object IRIs use the Clause Q.8 ``bacnet://`` scheme also written by
the BACpypes3 BACnet-to-RDF site scan (https://github.com/JoelBender/BACpypes3/pull/199):
``bacnet://<device-instance>`` and ``bacnet://<device-instance>/<type>,<instance>``.
Brick and 223P properties stay on the bacnet/2020 IRIs those ontologies declare;
a scan graph in bacnet/2020 or bacnet/2024 can be joined on the same object IRIs.
This module does not copy addresses, MAC values, or present-values from a scan.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from ..model.graph import valid_external_iri

BACNET = Namespace("http://data.ashrae.org/bacnet/2020#")
BACNET_2024 = Namespace("http://data.ashrae.org/bacnet/2024#")
BRICK_REF = Namespace("https://brickschema.org/schema/Brick/ref#")
S223 = Namespace("http://data.ashrae.org/standard223#")
VOCAB = Namespace("https://example.org/bask-223p/v2#")

# 223P SHACL: object-identifier and device-identifier are type,instance with instance >= 1.
IDENT_PATTERN = re.compile(r"^[A-Za-z0-9-]+,[1-9][0-9]*$")
MAX_INSTANCE = 4_194_303
SKIP_SCAN_TYPES = frozenset({"device", "network-port"})

_COL_ALIASES = {
    "device_instance": ("device_instance", "deviceid", "device_id", "device"),
    "object_type": ("object_type", "objtype", "obj_type"),
    "object_instance": ("object_instance", "objinst", "obj_inst", "object_id"),
    "object_name": ("object_name", "name", "objectname"),
    "description": ("description",),
    "device_name": ("device_name", "devicename"),
    "property_identifier": ("property_identifier", "property", "propertyid"),
    "brick_class": ("brick_class", "classuri", "brickclass"),
    "haystack_tags": ("haystack_tags", "haystack", "tags"),
    "property_array_index": ("property_array_index", "property-array-index", "array_index"),
    "priority_for_writing": ("priority_for_writing", "priority-for-writing", "write_priority", "priority"),
}

# Common BACnet object-type spellings -> hyphenated CSML / BACpypes3 form.
_TYPE_ALIASES = {
    "ai": "analog-input",
    "ao": "analog-output",
    "av": "analog-value",
    "bi": "binary-input",
    "bo": "binary-output",
    "bv": "binary-value",
    "msi": "multi-state-input",
    "mso": "multi-state-output",
    "msv": "multi-state-value",
}


def normalize_object_type(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("object type is required")
    key = raw.replace("_", "-").casefold()
    if key in _TYPE_ALIASES:
        return _TYPE_ALIASES[key]
    hyphen = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", raw)
    hyphen = hyphen.replace("_", "-").replace(" ", "-").casefold()
    hyphen = re.sub(r"-{2,}", "-", hyphen)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", hyphen):
        raise ValueError(f"invalid BACnet object type: {value!r}")
    return hyphen


def normalize_property_identifier(value: str | None) -> str:
    raw = (value or "present-value").strip() or "present-value"
    hyphen = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", raw)
    return hyphen.replace("_", "-").replace(" ", "-").casefold()



def parse_optional_uint(value: str, label: str, minimum: int = 0, maximum: int | None = None) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    if not text.isdigit():
        raise ValueError(f"invalid BACnet {label}: {value!r}")
    number = int(text)
    if number < minimum or (maximum is not None and number > maximum):
        raise ValueError(f"invalid BACnet {label}: {value!r}")
    return number


def parse_instance(value: str, label: str) -> int:
    text = (value or "").strip()
    if not text.isdigit() or not (0 <= int(text) <= MAX_INSTANCE):
        raise ValueError(f"invalid BACnet {label}: {value!r}")
    return int(text)


def object_identifier(object_type: str, object_instance: int) -> str:
    typ = normalize_object_type(object_type)
    return f"{typ},{object_instance}"


def device_identifier(device_instance: int) -> str:
    return f"device,{device_instance}"


def bacnet_object_uri(device_instance: int, object_type: str, object_instance: int) -> str:
    return f"bacnet://{device_instance}/{object_identifier(object_type, object_instance)}"


def bacnet_device_uri(device_instance: int) -> str:
    return f"bacnet://{device_instance}"


def _fits_ident(identifier: str) -> bool:
    return bool(IDENT_PATTERN.fullmatch(identifier))


@dataclass
class BacnetPoint:
    device_instance: int
    object_type: str
    object_instance: int
    object_name: str
    description: str = ""
    device_name: str = ""
    property_identifier: str = "present-value"
    brick_class: str = ""
    haystack_tags: str = ""
    property_array_index: int | None = None
    priority_for_writing: int | None = None
    source_row: int = 0
    # False when the name came from Niagara (point/device display name) rather than
    # from BACnet itself; then no bacnet:object-name / device-name is emitted, so a
    # merged scan + Niagara graph never carries two conflicting BACnet names.
    object_name_is_bacnet: bool = True
    device_name_is_bacnet: bool = True

    def __post_init__(self) -> None:
        self.object_type = normalize_object_type(self.object_type)
        self.object_instance = int(self.object_instance)
        self.device_instance = int(self.device_instance)
        self.property_identifier = normalize_property_identifier(self.property_identifier)
        self.object_name = (self.object_name or "").strip()
        if not self.object_name:
            raise ValueError("object name is required")
        if not (0 <= self.device_instance <= MAX_INSTANCE):
            raise ValueError(f"invalid BACnet device instance: {self.device_instance}")
        if not (0 <= self.object_instance <= MAX_INSTANCE):
            raise ValueError(f"invalid BACnet object instance: {self.object_instance}")
        if self.brick_class and not valid_external_iri(self.brick_class):
            raise ValueError(f"invalid brick_class IRI: {self.brick_class!r}")
        if self.property_array_index is not None and self.property_array_index < 0:
            raise ValueError(f"invalid BACnet property-array-index: {self.property_array_index}")
        if self.priority_for_writing is not None and not (1 <= self.priority_for_writing <= 16):
            raise ValueError(f"invalid BACnet priority-for-writing: {self.priority_for_writing}")

    @property
    def object_id(self) -> str:
        return object_identifier(self.object_type, self.object_instance)

    @property
    def device_id(self) -> str:
        return device_identifier(self.device_instance)

    @property
    def object_iri(self) -> str:
        return bacnet_object_uri(self.device_instance, self.object_type, self.object_instance)

    @property
    def device_iri(self) -> str:
        return bacnet_device_uri(self.device_instance)

    @property
    def bacnet_uri(self) -> str:
        return self.object_iri


def _header_map(fieldnames: list[str] | None) -> dict[str, str]:
    if not fieldnames:
        raise ValueError("CSV has no header row")
    index = {name.strip().casefold().replace(" ", "_"): name for name in fieldnames if name}
    mapping: dict[str, str] = {}
    for logical, aliases in _COL_ALIASES.items():
        for alias in aliases:
            if alias in index:
                mapping[logical] = index[alias]
                break
    missing = [key for key in ("device_instance", "object_type", "object_instance", "object_name") if key not in mapping]
    if missing:
        raise ValueError("CSV needs device_instance, object_type, object_instance, and object_name "
                         f"(missing {missing})")
    return mapping


def load_points_csv(path: Path) -> list[BacnetPoint]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = _header_map(reader.fieldnames)
        points: list[BacnetPoint] = []
        seen: dict[tuple[int, str, int], str] = {}
        for index, raw in enumerate(reader, start=2):
            def cell(logical: str) -> str:
                src = columns.get(logical)
                return (raw.get(src) or "").strip() if src else ""

            if not any(cell(k) for k in ("device_instance", "object_type", "object_instance", "object_name")):
                continue
            point = BacnetPoint(
                device_instance=parse_instance(cell("device_instance"), "device_instance"),
                object_type=cell("object_type"),
                object_instance=parse_instance(cell("object_instance"), "object_instance"),
                object_name=cell("object_name"),
                description=cell("description"),
                device_name=cell("device_name"),
                property_identifier=cell("property_identifier") or "present-value",
                brick_class=cell("brick_class"),
                haystack_tags=cell("haystack_tags"),
                property_array_index=parse_optional_uint(cell("property_array_index"), "property-array-index"),
                priority_for_writing=parse_optional_uint(cell("priority_for_writing"), "priority-for-writing", 1, 16),
                source_row=index,
            )
            key = (point.device_instance, point.object_type, point.object_instance)
            if key in seen:
                raise ValueError(f"Duplicate BACnet object {point.object_id} on device "
                                 f"{point.device_instance} (rows {seen[key]} and {point.object_name})")
            seen[key] = point.object_name
            points.append(point)
    if not points:
        raise ValueError(f"{path} has no BACnet point rows")
    return points


def _literal_text(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith("http://data.ashrae.org/bacnet/") and "#" in text:
        text = text.rsplit("#", 1)[-1]
        if text.startswith("ObjectType."):
            text = text.split(".", 1)[1]
    return text.strip()


def load_points_scan(path: Path) -> list[BacnetPoint]:
    """Lift object identifiers from a BACpypes3-style site-scan graph.

    Accepts bacnet/2020 or bacnet/2024 predicates. Device and network-port
    objects are skipped. Address, MAC, and present-value triples are ignored.
    """
    graph = Graph()
    graph.parse(path, format="turtle")
    points: list[BacnetPoint] = []
    seen: set[tuple[int, str, int]] = set()
    for namespace in (BACNET, BACNET_2024):
        for subject, ident in graph.subject_objects(namespace["object-identifier"]):
            ident_text = _literal_text(ident)
            if "," not in ident_text:
                continue
            typ, inst_text = ident_text.split(",", 1)
            typ = normalize_object_type(typ)
            if typ in SKIP_SCAN_TYPES:
                continue
            inst = parse_instance(inst_text, "object_instance")
            device_inst = _device_instance_for(graph, subject, typ, inst)
            name = _first_text(graph, subject, (namespace["object-name"],))
            if not name:
                continue
            desc = _first_text(graph, subject, (namespace["description"],))
            device_name = _device_name_for(graph, device_inst)
            key = (device_inst, typ, inst)
            if key in seen:
                continue
            seen.add(key)
            points.append(BacnetPoint(
                device_instance=device_inst,
                object_type=typ,
                object_instance=inst,
                object_name=name,
                description=desc,
                device_name=device_name,
            ))
    points.sort(key=lambda p: (p.device_instance, p.object_type, p.object_instance))
    if not points:
        raise ValueError(f"{path} has no BACnet objects with identifiers")
    return points


def _first_text(graph: Graph, subject, predicates) -> str:
    for pred in predicates:
        for value in graph.objects(subject, pred):
            text = _literal_text(value)
            if text:
                return text
    return ""


def _device_instance_for(graph: Graph, subject, typ: str, inst: int) -> int:
    iri = str(subject)
    match = re.match(r"bacnet://(\d+)/", iri)
    if match:
        return parse_instance(match.group(1), "device_instance")
    for namespace in (BACNET, BACNET_2024):
        for device in graph.subjects(namespace["contains"], subject):
            for value in graph.objects(device, namespace["device-instance"]):
                return parse_instance(_literal_text(value), "device_instance")
            dmatch = re.match(r"bacnet://(\d+)/?$", str(device))
            if dmatch:
                return parse_instance(dmatch.group(1), "device_instance")
    raise ValueError(f"cannot determine device instance for {typ},{inst}")


def _device_name_for(graph: Graph, device_instance: int) -> str:
    device = URIRef(bacnet_device_uri(device_instance))
    # Prefer an explicit device object name, never an address label.
    obj = URIRef(f"bacnet://{device_instance}/device,{device_instance}")
    for namespace in (BACNET, BACNET_2024):
        name = _first_text(graph, obj, (namespace["object-name"],))
        if name:
            return name
        name = _first_text(graph, device, (RDFS.label,))
        if name:
            return name
    return ""


def load_points(path: Path) -> list[BacnetPoint]:
    suffix = path.suffix.lower()
    if suffix in {".ttl", ".turtle", ".rdf", ".nt"}:
        return load_points_scan(path)
    return load_points_csv(path)


def _point_node(base: str, point: BacnetPoint) -> URIRef:
    key = f"{point.device_instance}|{point.object_id}|{point.object_name}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return URIRef(f"{base.rstrip('/')}/point/{digest}")


def _ref_node(base: str, point: BacnetPoint) -> URIRef:
    return reference_iri(base, point.device_instance, point.object_id)


def reference_iri(base: str, device_instance: int, object_id: str) -> URIRef:
    """Deterministic IRI of the external reference for one BACnet object.

    Depends only on the base and (device instance, object identifier), so a graph
    built from a BACpypes3 site scan and one built from baskStream/Niagara give the
    same object the same reference node.
    """
    key = f"ref|{device_instance}|{object_id}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return URIRef(f"{base.rstrip('/')}/bacnet-ref/{digest}")


def add_bacnet_device(graph: Graph, device_instance: int, device_name: str = "") -> URIRef:
    """``bacnet://<instance>`` device node, typed for bacnet/2020 and bacnet/2024 (PR 199 shape)."""
    device = URIRef(bacnet_device_uri(device_instance))
    graph.add((device, RDF.type, BACNET.BACnetDevice))
    graph.add((device, RDF.type, BACNET.Device))
    graph.add((device, RDF.type, BACNET_2024.Device))
    graph.add((device, BACNET["device-instance"], Literal(device_instance, datatype=XSD.nonNegativeInteger)))
    graph.add((device, BACNET_2024["device-instance"], Literal(device_instance, datatype=XSD.nonNegativeInteger)))
    if device_name:
        graph.add((device, RDFS.label, Literal(device_name)))
    return device


def add_bacnet_reference(graph: Graph, base: str, point: BacnetPoint, holder: URIRef) -> URIRef:
    """Attach the one shared external-reference shape to ``holder``.

    Emits, identically for every source (scan, CSV, baskStream):

    * ``s223:BACnetExternalReference`` + Brick ``ref:BACnetReference`` with
      ``bacnet:device-identifier`` (``device,N``; omitted for instance 0, which the
      223P pattern rejects), ``bacnet:object-identifier`` (``type,N``),
      ``bacnet:object-type``, ``bacnet:property-identifier``, ``bacnet:objectOf``
      the ``bacnet://N`` device and ``ref:BACnetURI``
      ``bacnet://<device>/<type>,<instance>`` (ASHRAE 135 Clause Q.8, the same IRI
      the BACpypes3 site scan uses for the object node);
    * optional ``property-array-index`` / ``priority-for-writing`` (223P 11.18.1);
    * ``bacnet:object-name`` / ``device-name`` only when they are BACnet names;
    * the scan-compatible object node ``bacnet://N/type,I`` with
      ``bacnet:contains`` from the device (no addresses or present-values).
    """
    ref = reference_iri(base, point.device_instance, point.object_id)
    device = URIRef(point.device_iri)
    obj = URIRef(point.object_iri)
    graph.add((holder, BRICK_REF.hasExternalReference, ref))
    graph.add((holder, S223.hasExternalReference, ref))

    graph.add((ref, RDF.type, BRICK_REF.BACnetReference))
    graph.add((ref, RDF.type, S223.BACnetExternalReference))
    graph.add((ref, RDF.type, S223.ExternalReference))
    graph.add((ref, RDF.type, S223.Concept))
    graph.add((ref, BACNET["object-identifier"], Literal(point.object_id)))
    graph.add((ref, BACNET["object-type"], Literal(point.object_type)))
    graph.add((ref, BACNET.objectOf, device))
    graph.add((ref, BRICK_REF.BACnetURI, Literal(point.bacnet_uri)))
    graph.add((ref, BACNET["property-identifier"], Literal(point.property_identifier)))
    if _fits_ident(point.device_id):
        graph.add((ref, BACNET["device-identifier"], Literal(point.device_id)))
    if point.object_name_is_bacnet and point.object_name:
        graph.add((ref, BACNET["object-name"], Literal(point.object_name)))
    if point.device_name_is_bacnet and point.device_name:
        graph.add((ref, BACNET["device-name"], Literal(point.device_name)))
    if point.description and point.object_name_is_bacnet:
        graph.add((ref, BACNET.description, Literal(point.description)))
    if point.property_array_index is not None:
        graph.add((ref, BACNET["property-array-index"],
                   Literal(point.property_array_index, datatype=XSD.nonNegativeInteger)))
    if point.priority_for_writing is not None:
        graph.add((ref, BACNET["priority-for-writing"],
                   Literal(point.priority_for_writing, datatype=XSD.nonNegativeInteger)))

    graph.add((device, BACNET.contains, obj))
    graph.add((device, BACNET_2024.contains, obj))
    graph.add((obj, RDF.type, BACNET_2024.Object))
    graph.add((obj, BACNET_2024["object-identifier"], Literal(point.object_id)))
    # BACpypes3 bacnet-rdf writes object-type as an enumeration IRI (bacnet:ObjectType.analog-input).
    graph.add((obj, BACNET_2024["object-type"], BACNET_2024[f"ObjectType.{point.object_type}"]))
    if point.object_name_is_bacnet and point.object_name:
        graph.add((obj, BACNET_2024["object-name"], Literal(point.object_name)))
    return ref


def build_reference_graph(points: list[BacnetPoint], base: str) -> tuple[Graph, dict]:
    if not valid_external_iri(base):
        raise ValueError("Base must be a fully qualified HTTP(S) or URN IRI")
    if not points:
        raise ValueError("No BACnet points to export")
    graph = Graph()
    graph.bind("bacnet", BACNET)
    graph.bind("bacnet2024", BACNET_2024)
    graph.bind("ref", BRICK_REF)
    graph.bind("s223", S223)
    graph.bind("oc", VOCAB)
    graph.bind("rdfs", RDFS)
    devices: dict[int, str] = {}
    for point in points:
        devices.setdefault(point.device_instance, point.device_name)
        if point.device_name and not devices[point.device_instance]:
            devices[point.device_instance] = point.device_name

    for instance, name in devices.items():
        add_bacnet_device(graph, instance, name)

    approved = 0
    for point in points:
        node = _point_node(base, point)
        graph.add((node, RDF.type, VOCAB.Point))
        graph.add((node, RDF.type, VOCAB.AddressedPoint))
        graph.add((node, RDFS.label, Literal(point.description or point.object_name)))
        graph.add((node, VOCAB.object_name, Literal(point.object_name)))
        add_bacnet_reference(graph, base, point, node)
        if point.brick_class:
            graph.add((node, RDF.type, URIRef(point.brick_class)))
            approved += 1
        if point.haystack_tags:
            graph.add((node, VOCAB.haystackTags, Literal(point.haystack_tags)))

    summary = {
        "points": len(points),
        "devices": len(devices),
        "triples": len(graph),
        "approved_brick_classes": approved,
        "model": "Brick ref:BACnetReference + 223P BACnetExternalReference; lexical types are not approved classes",
    }
    return graph, summary


def manifest_rows(points: list[BacnetPoint], base: str) -> list[dict[str, str]]:
    rows = []
    for point in points:
        rows.append({
            "point_iri": str(_point_node(base, point)),
            "device_instance": str(point.device_instance),
            "device_name": point.device_name,
            "device_identifier": point.device_id,
            "object_type": point.object_type,
            "object_instance": str(point.object_instance),
            "object_identifier": point.object_id,
            "object_name": point.object_name,
            "bacnet_uri": point.bacnet_uri,
            "property_identifier": point.property_identifier,
            "brick_class": point.brick_class,
            "haystack_tags": point.haystack_tags,
            "description": point.description,
        })
    return rows


def write_manifest(points: list[BacnetPoint], base: str, dest: Path) -> dict[str, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    rows = manifest_rows(points, base)
    csv_path = dest if dest.suffix.lower() == ".csv" else dest.with_suffix(".csv")
    json_path = csv_path.with_suffix(".json")
    fieldnames = list(rows[0].keys()) if rows else []
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return {"manifest_csv": str(csv_path), "manifest_json": str(json_path)}


def export_bacnet_refs(source: Path, base: str, out: Path, manifest: Path | None = None) -> dict:
    points = load_points(source)
    graph, summary = build_reference_graph(points, base)
    out.parent.mkdir(parents=True, exist_ok=True)
    graph.serialize(destination=str(out), format="turtle")
    summary["output"] = str(out)
    if manifest is not None:
        summary.update(write_manifest(points, base, manifest))
    return summary


LOCAL_POINT_TYPES = frozenset({
    str(VOCAB.Point), str(VOCAB.AddressedPoint),
})


def _point_brick_class(graph: Graph, point) -> str:
    if point is None:
        return ""
    for typ in graph.objects(point, RDF.type):
        iri = str(typ)
        if iri in LOCAL_POINT_TYPES:
            continue
        if iri.startswith("https://example.org/bask-223p/"):
            continue
        return iri
    return ""


def bindings_from_graph(graph: Graph) -> list[dict[str, str]]:
    """Rebuild the point-binding table from a Brick/223P reference graph."""
    refs = set(graph.subjects(RDF.type, BRICK_REF.BACnetReference))
    refs |= set(graph.subjects(RDF.type, S223.BACnetExternalReference))
    rows: list[dict[str, str]] = []
    seen: set = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        points = list(graph.subjects(BRICK_REF.hasExternalReference, ref))
        points += [p for p in graph.subjects(S223.hasExternalReference, ref) if p not in points]
        point = points[0] if points else None
        device = graph.value(ref, BACNET.objectOf)
        object_id = str(graph.value(ref, BACNET["object-identifier"]) or "")
        device_id = str(graph.value(ref, BACNET["device-identifier"]) or "")
        uri = str(graph.value(ref, BRICK_REF.BACnetURI) or "")
        object_name = str(graph.value(ref, BACNET["object-name"]) or "")
        if point and not object_name:
            object_name = str(graph.value(point, VOCAB.object_name) or graph.value(point, RDFS.label) or "")
        device_name = str(graph.value(ref, BACNET["device-name"]) or "")
        if not device_name and device is not None:
            device_name = str(graph.value(device, RDFS.label) or "")
        device_instance = ""
        object_type, object_instance = "", ""
        if "," in object_id:
            object_type, object_instance = object_id.split(",", 1)
        if device_id.startswith("device,") and device_id.split(",", 1)[1].isdigit():
            device_instance = device_id.split(",", 1)[1]
        elif device is not None:
            inst = graph.value(device, BACNET["device-instance"]) or graph.value(device, BACNET_2024["device-instance"])
            if inst is not None:
                device_instance = str(inst)
        if not uri and device_instance and object_id:
            uri = f"bacnet://{device_instance}/{object_id}"
        tags = ""
        if point is not None:
            tags = str(graph.value(point, VOCAB.haystackTags) or "")
        desc = str(graph.value(ref, BACNET.description) or "")
        if not desc and point is not None:
            desc = str(graph.value(point, RDFS.label) or "")
        rows.append({
            "point_iri": str(point or ""),
            "device_instance": device_instance,
            "device_name": device_name,
            "device_identifier": device_id,
            "object_type": object_type,
            "object_instance": object_instance,
            "object_identifier": object_id,
            "object_name": object_name,
            "bacnet_uri": uri,
            "property_identifier": str(graph.value(ref, BACNET["property-identifier"]) or ""),
            "property_array_index": str(graph.value(ref, BACNET["property-array-index"]) or ""),
            "priority_for_writing": str(graph.value(ref, BACNET["priority-for-writing"]) or ""),
            "brick_class": _point_brick_class(graph, point),
            "haystack_tags": tags,
            "description": desc,
        })
    rows.sort(key=lambda r: (r.get("device_instance") or "", r.get("object_identifier") or "", r.get("object_name") or ""))
    if not rows:
        raise ValueError("Graph has no Brick ref:BACnetReference or 223P BACnetExternalReference nodes")
    return rows


def export_bindings(source: Path, dest: Path) -> dict:
    """Write a Niagara-oriented point-binding CSV/JSON from a reference Turtle graph."""
    suffix = source.suffix.lower()
    if suffix not in {".ttl", ".turtle", ".rdf", ".nt"}:
        raise ValueError("export-bindings expects a Turtle/RDF graph; use export-bacnet-refs for CSV inventories")
    graph = Graph()
    graph.parse(source, format="turtle")
    rows = bindings_from_graph(graph)
    dest.parent.mkdir(parents=True, exist_ok=True)
    csv_path = dest if dest.suffix.lower() == ".csv" else dest.with_suffix(".csv")
    json_path = csv_path.with_suffix(".json")
    fieldnames = list(rows[0].keys())
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return {"bindings": len(rows), "manifest_csv": str(csv_path), "manifest_json": str(json_path)}
