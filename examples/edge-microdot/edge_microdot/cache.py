"""Hot latest-value cache (dict or DuckDB :memory:)."""
from __future__ import annotations

import threading
from typing import Any, Literal, Sequence

from edge_microdot.models import PointSample


def _split_value(val: Any) -> tuple[float | None, str | None]:
    if val is None:
        return None, None
    if isinstance(val, bool):
        return (1.0 if val else 0.0), ("true" if val else "false")
    if isinstance(val, (int, float)):
        return float(val), str(val)
    text = str(val)
    try:
        return float(text), text
    except ValueError:
        return None, text


class HotCache:
    """Thread-safe latest values keyed by ref (Normal Framework Redis pattern)."""

    def __init__(self, backend: Literal["dict", "duckdb"] = "dict") -> None:
        self.backend = backend
        self._lock = threading.Lock()
        self.last_updated_utc: str | None = None
        self._latest_dict: dict[str, PointSample] = {}
        self._duck: Any = None
        if backend == "duckdb":
            import duckdb

            self._duck = duckdb.connect(":memory:")
            self._duck.execute("SET TimeZone = 'UTC'")
            self._duck.execute("""
                CREATE TABLE hot_latest (
                    ref VARCHAR PRIMARY KEY,
                    timestamp_utc TIMESTAMPTZ NOT NULL,
                    site VARCHAR NOT NULL,
                    source VARCHAR NOT NULL,
                    device VARCHAR NOT NULL,
                    object VARCHAR NOT NULL,
                    property VARCHAR NOT NULL,
                    value_num DOUBLE,
                    value_text VARCHAR,
                    units VARCHAR,
                    status VARCHAR NOT NULL
                )
            """)

    def write(self, samples: Sequence[PointSample]) -> None:
        if not samples:
            return
        with self._lock:
            for s in samples:
                ref = s.ref or f"bacnet://{s.device}/{s.object}"
                self.last_updated_utc = s.timestamp_utc
                if self.backend == "dict":
                    self._latest_dict[ref] = s
                else:
                    v_num, v_txt = _split_value(s.value)
                    self._duck.execute(
                        """
                        INSERT INTO hot_latest VALUES (?, CAST(? AS TIMESTAMPTZ), ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (ref) DO UPDATE SET
                            timestamp_utc = excluded.timestamp_utc,
                            site = excluded.site,
                            source = excluded.source,
                            device = excluded.device,
                            object = excluded.object,
                            property = excluded.property,
                            value_num = excluded.value_num,
                            value_text = excluded.value_text,
                            units = excluded.units,
                            status = excluded.status
                        """,
                        [
                            ref,
                            s.timestamp_utc,
                            s.site,
                            s.source,
                            s.device,
                            s.object,
                            s.property,
                            v_num,
                            v_txt,
                            s.units,
                            s.status,
                        ],
                    )

    @property
    def count(self) -> int:
        with self._lock:
            if self.backend == "dict":
                return len(self._latest_dict)
            row = self._duck.execute("SELECT COUNT(*) FROM hot_latest").fetchone()
            return int(row[0]) if row else 0

    def snapshot_latest(self) -> list[dict[str, Any]]:
        with self._lock:
            if self.backend == "dict":
                return [s.to_dict() for s in self._latest_dict.values()]
            cur = self._duck.execute(
                """
                SELECT
                    ref,
                    strftime(timestamp_utc AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%gZ') AS timestamp_utc,
                    site, source, device, object, property, value_num, value_text, units, status
                FROM hot_latest
                ORDER BY ref
                """
            )
            cols = [d[0] for d in cur.description]
            out: list[dict[str, Any]] = []
            for row in cur.fetchall():
                rec = dict(zip(cols, row))
                rec["value"] = rec["value_num"] if rec["value_num"] is not None else rec["value_text"]
                out.append(rec)
            return out

    def snapshot_for_fdd(self) -> list[dict[str, Any]]:
        return self.snapshot_latest()

    def close(self) -> None:
        with self._lock:
            if self._duck is not None:
                self._duck.close()
                self._duck = None