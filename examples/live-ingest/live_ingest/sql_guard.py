"""Read-only SQL guard for the DuckDB SQL playground endpoint."""
from __future__ import annotations

import re

_ALLOWED_START_RE = re.compile(
    r"^\s*(?:SELECT|WITH|SHOW|DESCRIBE|EXPLAIN|SUMMARIZE)\b", re.IGNORECASE
)
_FORBIDDEN_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|REPLACE|TRUNCATE|ATTACH|DETACH|COPY|EXPORT|IMPORT|CALL|LOAD|INSTALL|PRAGMA|SET|VACUUM)\b",
    re.IGNORECASE,
)


def validate_read_only_sql(sql: str) -> str:
    text = (sql or "").strip()
    if not text:
        raise ValueError("SQL query is empty.")
    # Strip trailing semicolon only; reject multi-statement payloads.
    stripped = text.rstrip(";").strip()
    if ";" in stripped:
        raise ValueError("Only a single read-only SQL statement is permitted.")
    if not _ALLOWED_START_RE.match(stripped):
        raise ValueError("SQL must start with SELECT, WITH, SHOW, DESCRIBE, EXPLAIN, or SUMMARIZE.")
    if _FORBIDDEN_RE.search(stripped):
        raise ValueError("Mutating or DDL SQL keywords are forbidden in the read-only DuckDB playground.")
    return stripped
