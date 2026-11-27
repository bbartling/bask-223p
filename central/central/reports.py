"""Reconcile edges that share a bacnet:// ref, and validate the union."""
from __future__ import annotations

from pathlib import Path

from rdflib import Graph

from bask_223p.bacnet.export223 import validate_223p_references
from bask_223p.bacnet.reconcile import _sides_from_ref_graph, reconcile

from .fetch import safe_error
from .merge import Merged


def reconcile_edges(merged: Merged) -> dict:
    """Pair edges in list order. The first edge of a pair is ``left``.

    ``reconcile`` names its arguments niagara and scan. Here left is the first
    argument and right is the second. A matched row with the same name and the
    same QUDT unit is ``duplicate-collapsed``. A matched row with a different
    name or unit is ``conflicting``: both triples stay in the union.
    """
    prepared = []
    for site, edge, graph in merged.named:
        prepared.append({"site": site, "edge": edge, "sides": _sides_from_ref_graph(graph)})
    pairs = []
    for index, left in enumerate(prepared):
        left_keys = {side.key for side in left["sides"]}
        for right in prepared[index + 1 :]:
            right_keys = {side.key for side in right["sides"]}
            shared = left_keys & right_keys
            if not shared:
                continue
            raw = reconcile(left["sides"], right["sides"])
            rows = []
            for row in raw["rows"]:
                if row["status"] == "matched" and not row["name_differs"] and not row["unit_differs"]:
                    kind = "duplicate-collapsed"
                elif row["status"] == "matched":
                    kind = "conflicting"
                elif row["status"] == "missing_in_scan":
                    kind = "only_in_left"
                else:
                    kind = "only_in_right"
                rows.append(
                    {
                        "kind": kind,
                        "bacnet_uri": row["bacnet_uri"],
                        "object_identifier": row["object_identifier"],
                        "left_name": row["niagara_name"],
                        "right_name": row["bacnet_object_name"],
                        "left_unit": row["niagara_unit"],
                        "right_unit": row["bacnet_unit"],
                        "name_differs": row["name_differs"],
                        "unit_differs": row["unit_differs"],
                    }
                )
            pairs.append(
                {
                    "left": {"site": left["site"], "edge": left["edge"]},
                    "right": {"site": right["site"], "edge": right["edge"]},
                    "shared_bacnet_refs": len(shared),
                    "duplicate_collapsed": sum(1 for row in rows if row["kind"] == "duplicate-collapsed"),
                    "conflicting": sum(1 for row in rows if row["kind"] == "conflicting"),
                    "only_in_left": sum(1 for row in rows if row["kind"] == "only_in_left"),
                    "only_in_right": sum(1 for row in rows if row["kind"] == "only_in_right"),
                    "rows": rows,
                }
            )
    return {
        "join_key": "(device instance, object type, object instance) == bacnet://<device>/<type>,<instance>",
        "pairs": pairs,
    }


def _public_reference(report: dict) -> dict:
    return {
        "conforms": report.get("conforms"),
        "engine": report.get("engine"),
        "bacnet_external_references": report.get("bacnet_external_references"),
        "violations": report.get("violations") or [],
        "warnings": report.get("warnings") or [],
    }


def validate_union(graph: Graph, *, shapes: Path | None, ontology_dir: Path | None) -> dict:
    """Reference check always. Official 223P SHACL when the shapes file is set."""
    references = _public_reference(validate_223p_references(graph))
    shacl: dict = {"status": "skipped", "reason": "shapes path is unset"}
    if shapes is not None:
        try:
            from bask_223p.bacnet.export223 import validate_official_223p

            official = validate_official_223p(graph, shapes, ontology_dir=ontology_dir)
            violations = []
            for row in official.get("violations") or []:
                violations.append({key: row.get(key, "") for key in ("severity", "focus", "path", "message")})
            warnings = []
            for row in (official.get("warnings") or [])[:50]:
                warnings.append({key: row.get(key, "") for key in ("severity", "focus", "path", "message")})
            shacl = {
                "status": "ok",
                "conforms": official.get("conforms"),
                "engine": official.get("engine"),
                "violation_count": official.get("violation_count"),
                "warning_count": official.get("warning_count"),
                "violations": violations,
                "warnings": warnings,
                "shapes": shapes.name,
            }
        except Exception as exc:
            shacl = {"status": "error", "message": safe_error(exc), "shapes": str(shapes)}
    return {"references": references, "shacl": shacl}
