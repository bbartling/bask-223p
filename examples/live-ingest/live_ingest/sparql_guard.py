"""Read-only SPARQL guard for the optional /sparql query endpoint."""
from __future__ import annotations

import re

_UPDATE_RE = re.compile(
    r"(?i)(?:^|[\s;])(INSERT|DELETE|DROP|CLEAR|LOAD|CREATE|COPY|MOVE|ADD)\b"
)


def validate_read_only_sparql(query: str) -> str:
    text = (query or "").strip()
    if not text:
        raise ValueError("SPARQL query is empty.")
    if _UPDATE_RE.search(text):
        raise ValueError("Only read-only SPARQL queries (SELECT, ASK, CONSTRUCT, DESCRIBE) are permitted.")
    return text
