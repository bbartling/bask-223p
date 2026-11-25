"""Turn loosely structured workbooks and BACnet inventories into requirements rows.

The official requirements sheet uses a ``requirement`` column (see ``requirements.py``).
This module also accepts common inventory / field-dictionary / discovery layouts so a
reviewer can run the audit without first hand-rewriting the spreadsheet:

* row-oriented sheets with a field-name, term, point-name or coded-field column
* a transposed layout whose first column holds labels such as Description, Best fit,
  Closest term and Status, and whose other columns are the fields under review
* a field-list layout (category, field name, description, optional Brick / 223P / Haystack review columns)
* a wide field-as-columns layout (a row of field names, the next row of descriptions, optional group labels above)
* BACnet discovery CSVs with Name / Description / object-type columns

Sheet titles are not hard-coded. Nothing here writes back to the source workbook.
"""
from __future__ import annotations

import re
from pathlib import Path

from .requirements import COLUMNS, Requirement, _norm, _rows_from, read_requirements

# Columns that can stand in for ``requirement``.
TERM_HEADERS = {
    "requirement", "field_name", "field", "term", "metadata_item", "point_name",
    "point_code", "coded_field", "name", "table_name",
}
CATEGORY_HEADERS = {"category", "table_name", "table", "catalog", "sheet", "objtype", "object_type", "obj_type"}
DESCRIPTION_HEADERS = {"description", "table_description", "purpose", "definition", "notes", "comment"}
TRANSPOSE_LABELS = {
    "description": "description",
    "best fit": "reviewer_notes",
    "best-fit": "reviewer_notes",
    "closest term": "reviewer_notes",
    "closest": "reviewer_notes",
    "status": "reviewer_notes",
    "candidate": "candidate_uri",
    "candidate uri": "candidate_uri",
    "candidate_uri": "candidate_uri",
    "vocabulary": "vocabulary",
    "notes": "reviewer_notes",
    "reviewer notes": "reviewer_notes",
}
SKIP_VALUES = {"", "tbd", "n/a", "na", "none", "column1"}


def _clean(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _keep(value: str) -> bool:
    return _clean(value).casefold() not in SKIP_VALUES


def _looks_like_tag(value: str) -> bool:
    """Compact BAS tags such as SA-T or SAT-SP, not a plain-language label."""
    text = _clean(value)
    return bool(re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)+", text)) or (text.isupper() and 2 <= len(text) <= 16)


def _sheet_rows(path: Path) -> list[tuple[str, list[list[str]]]]:
    """Return (sheet_name, rows) for every sheet, or a single CSV 'sheet'."""
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            rows = [["" if v is None else str(v) for v in r] for r in ws.iter_rows(values_only=True)]
            out.append((ws.title or "sheet", rows))
        wb.close()
        return out
    return [(path.stem or "sheet", _rows_from(path))]


def _requirement_from_cells(cells: dict[str, str], row_no: int, sheet: str) -> Requirement | None:
    term = ""
    for key in ("requirement", "field_name", "field", "term", "metadata_item", "point_name",
                "point_code", "coded_field", "name", "table_name"):
        if _keep(cells.get(key, "")):
            term = _clean(cells[key])
            break
    if not term:
        return None
    name, desc = _clean(cells.get("name") or ""), _clean(cells.get("description") or "")
    if name and desc and term == name and _looks_like_tag(name) and not _looks_like_tag(desc):
        term, cells = desc, {**cells, "reviewer_notes": "; ".join(x for x in (cells.get("reviewer_notes", ""), f"tag: {name}") if x)}
    category = ""
    for key in ("category", "table_name", "table", "catalog", "objtype", "object_type", "obj_type"):
        if key == "table_name" and term == _clean(cells.get("table_name", "")) and cells.get("field_name"):
            continue
        if _keep(cells.get(key, "")):
            category = _clean(cells[key])
            break
    if not category:
        category = sheet
    description = ""
    for key in ("description", "table_description", "purpose", "definition"):
        if _keep(cells.get(key, "")):
            description = _clean(cells[key])
            break
    notes_parts = []
    for key in ("notes", "comment", "reviewer_notes"):
        if _keep(cells.get(key, "")) and _clean(cells[key]) != description:
            notes_parts.append(_clean(cells[key]))
    return Requirement(
        row=row_no,
        category=category,
        requirement=term,
        description=description,
        candidate_uri=_clean(cells.get("candidate_uri") or cells.get("uri") or ""),
        vocabulary=_clean(cells.get("vocabulary") or ""),
        reviewer_notes="; ".join(notes_parts),
        extra={k: v for k, v in cells.items() if k not in COLUMNS and _keep(v)},
    )


def _from_row_oriented(sheet: str, rows: list[list[str]]) -> list[Requirement]:
    header = [_norm(h) for h in rows[0]]
    if not any(h in TERM_HEADERS for h in header):
        return []
    # A BACnet inventory uses Name for the point, not a modeling 'requirement' heading.
    out: list[Requirement] = []
    for i, values in enumerate(rows[1:], start=2):
        cells = {h: (values[j].strip() if j < len(values) and values[j] is not None else "")
                 for j, h in enumerate(header) if h}
        if not any(_keep(v) for v in cells.values()):
            continue
        req = _requirement_from_cells(cells, i, sheet)
        if req is not None:
            out.append(req)
    return out


def _label_key(value: str) -> str:
    return " ".join(_clean(value).casefold().replace("_", " ").replace("-", " ").split())


def _from_transposed(sheet: str, rows: list[list[str]]) -> list[Requirement]:
    """Fields as columns; first column is a row label (Description, Best fit, Status, ...)."""
    if not rows or len(rows[0]) < 3:
        return []
    labels = [_label_key(r[0]) if r else "" for r in rows]
    if not any(lab in TRANSPOSE_LABELS for lab in labels[1:]):
        return []
    headers = [_clean(v) for v in rows[0][1:]]
    if sum(1 for h in headers if _keep(h)) < 2:
        return []
    # Map each data row label to a requirement field (notes accumulate).
    slots: list[tuple[str, str]] = []
    for r in rows[1:]:
        if not r:
            continue
        lab = _label_key(r[0])
        if lab in TRANSPOSE_LABELS:
            slots.append((lab, TRANSPOSE_LABELS[lab]))
    out: list[Requirement] = []
    for col, name in enumerate(headers):
        if not _keep(name):
            continue
        fields = {"requirement": name, "category": sheet, "description": "",
                  "candidate_uri": "", "vocabulary": "", "reviewer_notes": ""}
        notes: list[str] = []
        for r, (lab, dest) in zip((row for row in rows[1:] if row and _label_key(row[0]) in TRANSPOSE_LABELS), slots):
            value = _clean(r[col + 1] if len(r) > col + 1 else "")
            if not _keep(value):
                continue
            if dest == "reviewer_notes":
                notes.append(f"{lab}: {value}")
            elif dest == "description" and not fields["description"]:
                fields["description"] = value
            elif dest in fields and not fields[dest]:
                fields[dest] = value
        fields["reviewer_notes"] = "; ".join(notes)
        out.append(Requirement(row=col + 2, extra={"layout": "transposed"}, **fields))
    return out



VOCAB_LABELS = {
    "brick": "Brick",
    "223p": "223P",
    "223": "223P",
    "ashrae 223p": "223P",
    "haystack": "Haystack",
    "bacnet": "BACnet",
    "best fit": "Best fit",
    "closest term": "Closest term",
    "status": "Status",
}


def _is_vocab_label(value: str) -> str | None:
    lab = _label_key(value)
    if lab in VOCAB_LABELS:
        return VOCAB_LABELS[lab]
    if "need extending" in lab:
        return "Need extending"
    return None


def _from_field_list(sheet: str, rows: list[list[str]]) -> list[Requirement]:
    """Category + field name + description rows, with optional vocabulary review columns.

    Typical header: a row naming Brick / 223P / Haystack (or Best fit / Closest term /
    Status). The field title often sits in a 'Notes' column; categories are in the first
    column and are forward-filled.
    """
    header_idx = None
    for i, row in enumerate(rows):
        hits = sum(1 for c in row if _is_vocab_label(c) or "need extending" in _label_key(c))
        if hits >= 2:
            header_idx = i
            break
    start = header_idx + 1 if header_idx is not None else None
    if start is None:
        for i, row in enumerate(rows):
            if len(row) > 1 and _keep(row[1]) and len(_clean(row[1])) <= 60:
                follow = sum(1 for rr in rows[i:i + 20] if len(rr) > 1 and _keep(rr[1]) and len(_clean(rr[1])) <= 60)
                if follow >= 6:
                    start = i
                    header_idx = i - 1 if i else None
                    break
    if start is None or start >= len(rows):
        return []

    name_col, desc_col, cat_col = 1, 2, 0
    vocab_cols: dict[int, str] = {}
    extra_cols: dict[int, str] = {}
    header_rows = []
    if header_idx is not None and header_idx >= 0:
        header_rows.append(rows[header_idx])
        if header_idx > 0:
            header_rows.insert(0, rows[header_idx - 1])
    for header in header_rows:
        for j, cell in enumerate(header):
            lab = _label_key(cell)
            vocab = _is_vocab_label(cell)
            if vocab:
                vocab_cols[j] = vocab
            elif lab in {"description", "general notes on item", "general notes", "definition", "general notes on item"}:
                desc_col = j
            elif lab in {"notes", "field", "field name", "term", "metadata item", "item"}:
                name_col = j
            elif lab in {"category", "group"}:
                cat_col = j
            elif lab in {"data ingest method", "data injest method", "ingest method", "end user", "source"}:
                extra_cols[j] = _clean(cell)

    data_hits = sum(1 for rr in rows[start:] if len(rr) > name_col and _keep(rr[name_col]) and len(_clean(rr[name_col])) <= 80)
    if data_hits < 6:
        return []

    out: list[Requirement] = []
    category = ""
    for i, row in enumerate(rows[start:], start=start + 1):
        while len(row) <= max(name_col, desc_col, cat_col):
            row = list(row) + [""]
        cat_val = _clean(row[cat_col]) if cat_col < len(row) else ""
        if _keep(cat_val) and cat_col != name_col:
            category = cat_val
        name = _clean(row[name_col]) if name_col < len(row) else ""
        if not _keep(name) or _is_vocab_label(name):
            continue
        desc = _clean(row[desc_col]) if desc_col < len(row) else ""
        notes: list[str] = []
        extra: dict[str, str] = {"layout": "field_list"}
        for j, label in {**vocab_cols, **extra_cols}.items():
            value = _clean(row[j]) if j < len(row) else ""
            if _keep(value):
                notes.append(f"{label}: {value}")
                extra[label.casefold().replace(" ", "_")] = value
        out.append(Requirement(
            row=i, category=category or sheet, requirement=name, description=desc,
            candidate_uri="", vocabulary="", reviewer_notes="; ".join(notes), extra=extra,
        ))
    return out


def _from_wide_fields(sheet: str, rows: list[list[str]]) -> list[Requirement]:
    """A row of many field names, the next row of descriptions, optional group labels above."""
    def _is_prompt_row(row: list[str]) -> bool:
        filled = [_clean(c) for c in row if _keep(c)]
        if not filled:
            return False
        starts = ("what ", "where ", "when ", "how ", "why ", "whose ", "who ")
        prompts = sum(1 for c in filled if c.casefold().startswith(starts) or c.endswith("?"))
        return prompts >= max(1, (len(filled) + 1) // 2)

    name_idx = None
    for i in range(len(rows) - 1):
        names, descs = rows[i], rows[i + 1]
        n_names = sum(1 for c in names if _keep(c))
        n_descs = sum(1 for c in descs if _keep(c))
        if n_names < 12 or n_descs < 12:
            continue
        avg_name = sum(len(_clean(c)) for c in names if _keep(c)) / n_names
        avg_desc = sum(len(_clean(c)) for c in descs if _keep(c)) / n_descs
        if avg_name <= 40 and avg_desc >= 6 and avg_desc >= avg_name - 2:
            name_idx = i
            break
    if name_idx is None:
        return []
    names, descs = rows[name_idx], rows[name_idx + 1]
    cats = [""] * len(names)
    for j in range(name_idx - 1, -1, -1):
        if _is_prompt_row(rows[j]):
            continue
        filled = sum(1 for c in rows[j] if _keep(c))
        if 1 <= filled <= 15:
            current = ""
            width = max(len(names), len(rows[j]))
            for k in range(width):
                val = _clean(rows[j][k]) if k < len(rows[j]) else ""
                if _keep(val):
                    current = val
                if k < len(cats):
                    cats[k] = current
            break
    out: list[Requirement] = []
    for col, raw in enumerate(names):
        name = _clean(raw)
        if not _keep(name):
            continue
        desc = _clean(descs[col]) if col < len(descs) else ""
        out.append(Requirement(
            row=col + 2, category=(cats[col] if col < len(cats) and cats[col] else sheet),
            requirement=name, description=desc, candidate_uri="", vocabulary="",
            reviewer_notes="", extra={"layout": "wide_fields"},
        ))
    return out


def ingest_workbook(path: str | Path) -> list[Requirement]:
    """Extract requirement rows from every sheet that matches a known layout."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    found: list[Requirement] = []
    seen: set[str] = set()
    for sheet, rows in _sheet_rows(path):
        nonempty = [r for r in rows if any(_keep(c) for c in r)]
        if not nonempty:
            continue
        extracted = (_from_row_oriented(sheet, nonempty) or _from_transposed(sheet, nonempty)
                     or _from_field_list(sheet, nonempty) or _from_wide_fields(sheet, nonempty))
        for req in extracted:
            key = req.requirement.casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append(req)
    if not found:
        raise ValueError(
            f"{path}: no requirement-like rows found. Expected a 'requirement' or 'field name' column, "
            "a BACnet Name column, a field-list (category / name / description), "
            "a wide field-as-columns sheet, or a transposed sheet with Description / Best fit / Status row labels."
        )
    # Number rows in extraction order so reports stay stable.
    for i, req in enumerate(found, start=2):
        req.row = i
    return found


def load_requirements(path: str | Path) -> list[Requirement]:
    """Read an official requirements sheet, or fall back to ``ingest_workbook``."""
    path = Path(path)
    try:
        # Official layout: first sheet has a requirement column.
        rows = _rows_from(path)
        if rows and "requirement" in {_norm(h) for h in rows[0]}:
            return read_requirements(path)
    except ValueError:
        pass
    return ingest_workbook(path)


def write_requirements_csv(rows: list[Requirement], dest: Path) -> Path:
    """Write the official requirements CSV so a reviewer can edit candidate URIs."""
    import csv

    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow({k: getattr(r, k) for k in COLUMNS})
    return dest
