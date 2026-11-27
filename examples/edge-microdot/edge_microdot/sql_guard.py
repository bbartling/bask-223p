"""Read-only SQL guard for the server-side DuckDB endpoint."""
from __future__ import annotations

import re

_ALLOWED_START_RE = re.compile(
    r"^\s*(?:SELECT|WITH|SHOW|DESCRIBE|EXPLAIN|SUMMARIZE)\b", re.IGNORECASE
)
_FORBIDDEN_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|REPLACE|TRUNCATE|ATTACH|DETACH|COPY|EXPORT|IMPORT|CALL|LOAD|INSTALL|PRAGMA|SET|VACUUM)\b",
    re.IGNORECASE,
)


def _without_comments(sql: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", without_block).strip()


def validate_read_only_sql(sql: str) -> str:
    text = (sql or "").strip()
    if not text:
        raise ValueError("SQL query is empty.")
    stripped = text.rstrip(";").strip()
    visible = _without_comments(stripped)
    if ";" in visible:
        raise ValueError("Only a single read-only SQL statement is permitted.")
    if not _ALLOWED_START_RE.match(visible):
        raise ValueError("SQL must start with SELECT, WITH, SHOW, DESCRIBE, EXPLAIN, or SUMMARIZE.")
    if _FORBIDDEN_RE.search(visible):
        raise ValueError("Mutating or DDL SQL keywords are forbidden.")
    return stripped
