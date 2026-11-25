"""Workbook ingest, gap report statuses, and draft extension stubs."""
from __future__ import annotations

from bask_223p.audit.gap import GapTerm, gap_status, propose_stubs, write_gap_report
from bask_223p.audit.ingest import ingest_workbook, load_requirements, write_requirements_csv


def test_ingest_field_list_with_vocab_columns(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Fields"
    ws.append(["", "", "", "Need Extending on Ontology?", "", "", "Ingest Method"])
    ws.append(["", "Notes", "General notes on item", "Brick", "223P", "Haystack", "Ingest Method"])
    ws.append(["Physical", "Manufacturer", "Brand identifier", "", "yes", "", "CMMS"])
    ws.append(["", "Model", "Specific unit model", "", "", "", "CMMS"])
    ws.append(["Spatial", "Campus ID", "Site or campus code", "partial", "", "", "front end"])
    ws.append(["", "Building ID", "Building number", "", "", "", "front end"])
    ws.append(["", "Floor ID", "Floor or space id", "", "", "", "front end"])
    ws.append(["Network", "IP Address", "Primary network port address", "", "", "", "scan"])
    path = tmp_path / "field-list.xlsx"
    wb.save(path)
    rows = ingest_workbook(path)
    by_name = {r.requirement: r for r in rows}
    assert set(by_name) >= {"Manufacturer", "Model", "Campus ID", "Building ID", "Floor ID", "IP Address"}
    assert by_name["Manufacturer"].category == "Physical"
    assert by_name["Model"].category == "Physical"
    assert by_name["Campus ID"].category == "Spatial"
    assert "Brand identifier" in by_name["Manufacturer"].description
    assert "223P: yes" in by_name["Manufacturer"].reviewer_notes
    assert "Brick: partial" in by_name["Campus ID"].reviewer_notes


def test_ingest_wide_field_columns(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Draft"
    ws.append(["Metadata draft"])
    ws.append(["Overview of proposed fields"])
    ws.append([])
    names = ["Manufacturer", "Model", "Size", "Size Unit", "System", "Serial Number",
             "Campus ID", "Building ID", "Floor ID", "Room Number", "Room Location", "Asset ID"]
    descs = [
        "Brand identifier", "Specific unit model", "Capacity as a number", "Unit of size",
        "System classification", "Manufacturer serial", "Campus or site code", "Building number",
        "Floor or space id", "Room or zone id", "Optional location in the room", "Optional asset tag",
    ]
    ws.append(["Physical"] + [""] * 5 + ["Spatial"] + [""] * 5)
    ws.append(["What is it"] + [""] * 5 + ["Where is it"] + [""] * 5)
    ws.append(names)
    ws.append(descs)
    path = tmp_path / "wide.xlsx"
    wb.save(path)
    rows = ingest_workbook(path)
    by_name = {r.requirement: r for r in rows}
    assert len(rows) == 12
    assert by_name["Manufacturer"].category == "Physical"
    assert by_name["Campus ID"].category == "Spatial"
    assert by_name["Building ID"].description == "Building number"


def test_gap_status_never_covers_lexical_hits(tmp_path):
    lexical = GapTerm(
        row=2, category="Point", requirement="Mixed air temperature",
        audit_status="exact_label_REVIEW",
        candidates=[{"uri": "https://example.org/Mixed_Air_Temperature_Sensor",
                     "score": 1.0, "label": "Mixed Air Temperature Sensor", "ontology": "Brick"}],
    )
    missing = GapTerm(row=3, category="Inventory", requirement="Asset Tag", audit_status="unmapped")
    approved = GapTerm(
        row=4, category="Point", requirement="Supply air temperature sensor",
        approved_uri="https://brickschema.org/schema/Brick#Supply_Air_Temperature_Sensor",
        approved_vocabulary="Brick",
    )
    assert gap_status(lexical) == "partial"
    assert gap_status(missing) == "needs_extension"
    assert gap_status(approved) == "covered"
    paths = write_gap_report([missing], tmp_path)
    assert "needs_extension" in paths["gap_md"].read_text(encoding="utf-8")
    stubs = propose_stubs([missing], tmp_path / "extensions")
    assert "ex:AssetTag a owl:Class" in stubs["proposed_classes"].read_text(encoding="utf-8")


def test_load_requirements_still_reads_official_csv(tmp_path):
    src = tmp_path / "official.csv"
    src.write_text(
        "category,requirement,description,candidate_URI,vocabulary,reviewer_notes\n"
        "Point,Supply air temperature sensor,x,brick:Supply_Air_Temperature_Sensor,Brick,\n",
        encoding="utf-8",
    )
    rows = load_requirements(src)
    assert len(rows) == 1 and rows[0].candidate_uri == "brick:Supply_Air_Temperature_Sensor"
    dest = write_requirements_csv(rows, tmp_path / "roundtrip.csv")
    assert "Supply air temperature sensor" in dest.read_text(encoding="utf-8")


def test_cli_audit_writes_gap_report(tmp_path):
    from bask_223p.cli import main

    ont = tmp_path / "mini.ttl"
    ont.write_text(
        '@prefix ex: <https://example.com/> .\n'
        '@prefix owl: <http://www.w3.org/2002/07/owl#> .\n'
        '@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n'
        'ex:Chiller a owl:Class; rdfs:label "Chiller" .\n',
        encoding="utf-8",
    )
    req = tmp_path / "terms.csv"
    req.write_text("Requirement\nChiller\nUnknownAssetTag\n", encoding="utf-8")
    out = tmp_path / "audit"
    assert main(["audit", "--requirements", str(req), "--catalog", f"test={ont}", "--out", str(out)]) == 0
    text = (out / "gap-report.csv").read_text(encoding="utf-8")
    assert "Chiller" in text and "partial" in text
    assert "UnknownAssetTag" in text and "needs_extension" in text
    assert (out / "extensions" / "proposed-classes.ttl").exists()
