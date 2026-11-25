"""Batched DuckDB file store with oldest-first size cap (SD-card friendly)."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Sequence

from edge_microdot.cache import _split_value
from edge_microdot.models import PointSample


class HistoryStore:
    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = 100 * 1024 * 1024,
        batch_size: int = 100,
    ) -> None:
        import duckdb

        self.path = Path(path)
        self.max_bytes = max(1, int(max_bytes))
        self.batch_size = max(1, int(batch_size))
        self._lock = threading.Lock()
        self._pending: list[tuple] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.path))
        self.conn.execute("SET TimeZone = 'UTC'")
        self._ensure_schema()
        self._maybe_prune_locked()

    def _ensure_schema(self) -> None:
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

    def _file_size(self) -> int:
        if not self.path.exists():
            return 0
        return self.path.stat().st_size

    def _maybe_prune_locked(self) -> None:
        while self._file_size() > self.max_bytes:
            self.conn.execute(
                """
                DELETE FROM telemetry
                WHERE rowid IN (
                    SELECT rowid FROM telemetry ORDER BY timestamp_utc ASC LIMIT 500
                )
                """
            )
            if self._file_size() <= self.max_bytes:
                break
            # Safety: stop if delete did not shrink (empty table)
            cnt = self.conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
            if cnt == 0:
                break

    def stage(self, samples: Sequence[PointSample]) -> None:
        if not samples:
            return
        rows = []
        for s in samples:
            v_num, v_txt = _split_value(s.value)
            ref = s.ref or f"bacnet://{s.device}/{s.object}"
            rows.append(
                (
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
                )
            )
        with self._lock:
            self._pending.extend(rows)
            if len(self._pending) >= self.batch_size:
                self._flush_locked()

    def flush(self) -> int:
        with self._lock:
            return self._flush_locked()

    def _flush_locked(self) -> int:
        if not self._pending:
            return 0
        batch = self._pending
        self._pending = []
        self.conn.executemany(
            """
            INSERT INTO telemetry (
                ref, timestamp_utc, site, source, device, object, property,
                value_num, value_text, units, status
            ) VALUES (?, CAST(? AS TIMESTAMPTZ), ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
        self._maybe_prune_locked()
        return len(batch)

    def query_history(
        self,
        *,
        ref: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
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
                site, source, device, object, property,
                value_num, value_text, units, status
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

    def close(self) -> None:
        with self._lock:
            self._flush_locked()
            self.conn.close()