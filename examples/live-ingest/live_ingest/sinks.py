"""Pluggable output sinks for live telemetry samples (Stdout, CSV, DuckDB, Memory)."""
from __future__ import annotations

import csv
import json
import sys
import threading
from collections import deque
from pathlib import Path
from typing import IO, Any, Protocol, Sequence, runtime_checkable

from .models import PointSample
from .sql_guard import validate_read_only_sql

CSV_COLUMNS = (
    "timestamp_utc",
    "site",
    "source",
    "device",
    "object",
    "property",
    "value",
    "units",
    "status",
)


@runtime_checkable
class Sink(Protocol):
    """Protocol for telemetry sinks (CSV, stdout, DuckDB, or data lake)."""

    def write(self, samples: Sequence[PointSample]) -> None: ...

    def close(self) -> None: ...


class StdoutSink:
    """Writes one line per sample to stdout (text or JSON lines)."""

    def __init__(self, *, fmt: str = "line", stream: IO[str] | None = None) -> None:
        self.fmt = fmt
        self.stream = stream or sys.stdout
        self._lock = threading.Lock()

    def write(self, samples: Sequence[PointSample]) -> None:
        if not samples:
            return
        with self._lock:
            for s in samples:
                if self.fmt == "json":
                    self.stream.write(json.dumps(s.to_dict(), default=str) + "\n")
                else:
                    val = "" if s.value is None else str(s.value)
                    unit = f" {s.units}" if s.units else ""
                    self.stream.write(
                        f"{s.timestamp_utc} | {s.source} | {s.ref or s.device} | "
                        f"{s.object}.{s.property} = {val}{unit} [{s.status}]\n"
                    )
            self.stream.flush()

    def close(self) -> None:
        pass


class CsvSink:
    """Thread-safe local CSV sink following the phase-2 ingest schema."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, samples: Sequence[PointSample]) -> None:
        if not samples:
            return
        with self._lock:
            needs_header = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                if needs_header:
                    writer.writerow(CSV_COLUMNS)
                for s in samples:
                    writer.writerow([
                        s.timestamp_utc,
                        s.site,
                        s.source,
                        s.device,
                        s.object,
                        s.property,
                        "" if s.value is None else s.value,
                        s.units,
                        s.status,
                    ])

    def close(self) -> None:
        pass


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


class DuckDbSink:
    """Local DuckDB time-series store keyed by ``bacnet://`` ref + timestamp."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        import duckdb

        self.db_path = str(path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = duckdb.connect(self.db_path)
        self.conn.execute("SET TimeZone = 'UTC'")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry (
                ref VARCHAR NOT NULL,
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
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telemetry_ref_ts ON telemetry (ref, timestamp_utc)"
        )

    def write(self, samples: Sequence[PointSample]) -> None:
        if not samples:
            return
        rows = []
        for s in samples:
            v_num, v_txt = _split_value(s.value)
            ref = s.ref or f"bacnet://{s.device}/{s.object}"
            rows.append((
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
            ))
        with self._lock:
            self.conn.executemany(
                """
                INSERT INTO telemetry (
                    ref, timestamp_utc, site, source, device, object, property,
                    value_num, value_text, units, status
                ) VALUES (?, CAST(? AS TIMESTAMPTZ), ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def query_history(
        self,
        *,
        ref: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if ref:
            clauses.append("ref = ?")
            params.append(ref)
        if start:
            clauses.append("timestamp_utc >= CAST(? AS TIMESTAMPTZ)")
            params.append(start)
        if end:
            clauses.append("timestamp_utc <= CAST(? AS TIMESTAMPTZ)")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT
                ref,
                strftime(timestamp_utc AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%gZ') AS timestamp_utc,
                site,
                source,
                device,
                object,
                property,
                value_num,
                value_text,
                units,
                status
            FROM telemetry
            {where}
            ORDER BY timestamp_utc ASC
            LIMIT ?
        """
        params.append(int(limit))
        with self._lock:
            cur = self.conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            rec = dict(zip(cols, row))
            rec["value"] = rec["value_num"] if rec["value_num"] is not None else rec["value_text"]
            out.append(rec)
        return out

    def query_sql(self, sql: str, *, max_rows: int = 1000) -> dict[str, Any]:
        clean = validate_read_only_sql(sql)
        with self._lock:
            cur = self.conn.execute(clean)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows_raw = cur.fetchmany(max_rows + 1)
        truncated = len(rows_raw) > max_rows
        rows_raw = rows_raw[:max_rows]
        rows = [
            {
                col: (val.isoformat() if hasattr(val, "isoformat") else val)
                for col, val in zip(cols, r)
            }
            for r in rows_raw
        ]
        return {
            "columns": cols,
            "count": len(rows),
            "truncated": truncated,
            "rows": rows,
        }

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class MemoryRingSink:
    """In-memory latest-value table and bounded ring buffer for the FastAPI UI/WS."""

    def __init__(self, max_history: int = 2000) -> None:
        self._lock = threading.Lock()
        self.latest: dict[tuple[str, str, str], PointSample] = {}
        self.history: deque[PointSample] = deque(maxlen=max_history)
        self.last_updated_utc: str | None = None

    def write(self, samples: Sequence[PointSample]) -> None:
        if not samples:
            return
        with self._lock:
            for s in samples:
                key = (s.device, s.object, s.property)
                self.latest[key] = s
                self.history.append(s)
                self.last_updated_utc = s.timestamp_utc

    def snapshot_latest(self) -> list[dict]:
        with self._lock:
            return [s.to_dict() for s in self.latest.values()]

    def snapshot_history(
        self,
        *,
        ref: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        with self._lock:
            items = list(self.history)
        if ref:
            items = [s for s in items if s.ref == ref]
        if start:
            items = [s for s in items if s.timestamp_utc >= start]
        if end:
            items = [s for s in items if s.timestamp_utc <= end]
        return [s.to_dict() for s in items[-limit:]]

    def close(self) -> None:
        pass


class MultiSink:
    """Fans out each batch of samples to multiple downstream Sink objects."""

    def __init__(self, sinks: Sequence[Sink]) -> None:
        self.sinks = list(sinks)

    def write(self, samples: Sequence[PointSample]) -> None:
        for sink in self.sinks:
            sink.write(samples)

    def close(self) -> None:
        for sink in self.sinks:
            sink.close()
