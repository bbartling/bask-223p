"""Two edges that both describe one AHU. Used by the worked example."""
from __future__ import annotations

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from bask_223p.bacnet.refs import VOCAB, BacnetPoint, build_reference_graph

from .merge import Merged, merge_models
from .reports import reconcile_edges, validate_union

BASE = "https://example.org/bench"
HAS_UNIT = URIRef("http://qudt.org/schema/qudt/hasUnit")
NOTE = URIRef("https://example.org/ns#Note")


def _point(object_type: str, instance: int, name: str, description: str, unit: str, tags: str = "") -> BacnetPoint:
    return BacnetPoint(
        device_instance=5007,
        device_name="AHU-1",
        object_type=object_type,
        object_instance=instance,
        object_name=name,
        description=description,
        property_identifier="present-value",
        qudt_unit=unit,
        haystack_tags=tags,
    )


def _with_units(graph: Graph, units: dict[str, str]) -> None:
    for name, unit in units.items():
        holders = list(graph.subjects(VOCAB.object_name, Literal(name)))
        for holder in holders:
            graph.add((holder, HAS_UNIT, URIRef(f"http://qudt.org/vocab/unit/{unit}")))


def _note(graph: Graph, label: str) -> None:
    node = BNode()
    graph.add((node, RDF.type, NOTE))
    graph.add((node, RDFS.label, Literal(label)))


def edge_graphs() -> tuple[Graph, Graph]:
    """Pi 1 read the controller. Pi 2 read the same controller through a station.

    Shared points use the same ``bacnet://`` IRI. One point disagrees on the
    QUDT unit. One disagrees on the BACnet object name. Each side has a point
    the other does not, and each side has its own blank node.
    """
    shared = [
        ("analog-input", 1, "SA-T", "Supply air temperature", "DEG_C"),
        ("analog-input", 2, "RA-T", "Return air temperature", "DEG_C"),
        ("analog-input", 3, "MA-T", "Mixed air temperature", "DEG_C"),
    ]
    bacnet_rows = [_point(*row, tags="point sensor temp" if row[2] == "SA-T" else "") for row in shared]
    bacnet_rows.append(_point("analog-input", 4, "DAP-P", "Duct static pressure", "PA"))
    bacnet_rows.append(_point("analog-output", 1, "SF-O", "Supply fan speed command", "PERCENT"))
    niagara_rows = [_point(*row) for row in shared]
    niagara_rows.append(_point("analog-input", 4, "DuctStatic", "Duct static pressure", "PA"))
    niagara_rows.append(_point("analog-input", 5, "OA-T", "Outside air temperature", "DEG_C"))

    bacnet, _summary = build_reference_graph(bacnet_rows, BASE)
    niagara, _summary = build_reference_graph(niagara_rows, BASE)
    bacnet_units = {"SA-T": "DEG_C", "RA-T": "DEG_C", "MA-T": "DEG_C", "DAP-P": "PA", "SF-O": "PERCENT"}
    niagara_units = {"SA-T": "DEG_C", "RA-T": "DEG_C", "MA-T": "DEG_F", "DuctStatic": "PA", "OA-T": "DEG_C"}
    _with_units(bacnet, bacnet_units)
    _with_units(niagara, niagara_units)
    _note(bacnet, "bacnet edge note")
    _note(niagara, "niagara edge note")
    return bacnet, niagara


def build(shapes=None, ontology_dir=None) -> tuple[Graph, Graph, Merged, dict, dict]:
    bacnet, niagara = edge_graphs()
    parts = [
        ("bench", "pi-bacnet", bacnet.serialize(format="turtle").encode("utf-8")),
        ("bench", "pi-niagara", niagara.serialize(format="turtle").encode("utf-8")),
    ]
    merged = merge_models(parts)
    report = reconcile_edges(merged)
    validation = validate_union(merged.union, shapes=shapes, ontology_dir=ontology_dir)
    return bacnet, niagara, merged, report, validation
