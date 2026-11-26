"""In-memory DuckDB history store with compact rotating Parquet snapshots."""
from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from edge_microdot.cache import HotCache, _split_value
from edge_microdot.models import PointSample, PointTarget, utc_now_iso
from edge_microdot.units_si import normalize_si_unit, to_si

LOG = logging.getLogger(__name__)


def _sql_quote(path: Path) -> str:
    return str(path).replace("'", "''")


def _target_equipment(t: PointTarget | None, ref: str = "") -> str:
    if t is not None:
        extra_eq = (t.extra or {}).get("equipment")
        if extra_eq:
            return str(extra_eq)
        if t.ord_path:
            parts = [p for p in t.ord_path.split("/") if p and not p.startswith("slot:")]
            if "points" in parts:
                idx = parts.index("points")
                if idx > 0:
                    return parts[idx - 1]
        if t.device:
            return f"device-{t.device}"
    if ref.startswith("bacnet://"):
        dev = ref[len("bacnet://") :].split("/", 1)[0]
        if dev:
            return f"device-{dev}"
    if ref.startswith("niagara://"):
        return "niagara"
    return "edge"


def _target_haystack_tags(t: PointTarget) -> str:
    extra = t.extra or {}
    tags = extra.get("haystack_tags") or extra.get("tags") or extra.get("role") or ""
    if isinstance(tags, (list, tuple, set)):
        return " ".join(str(x).strip() for x in tags if str(x).strip())
    return str(tags).strip()


def _target_223p_class(t: PointTarget, graph: Any = None) -> str:
    extra = t.extra or {}
    cls = extra.get("class_223p") or extra.get("s223_class") or ""
    if cls:
        return str(cls).strip()
    if graph is not None and t.point_uri:
        try:
            from rdflib import URIRef
            from rdflib.namespace import RDF

            s223_ns = "http://data.ashrae.org/standard223#"
            classes: list[str] = []
            for obj in graph.objects(URIRef(t.point_uri), RDF.type):
                s = str(obj)
                if s.startswith(s223_ns):
                    classes.append("s223:" + s[len(s223_ns) :])
            if classes:
                return ",".join(sorted(set(classes)))
        except Exception:
            pass
    return ""


class HistoryStore:
    """In-memory DuckDB telemetry + FDD store backed by capped Parquet snapshots."""

    def __init__(
        self,
        path: str | Path = "var/snapshots",
        *,
        max_bytes: int = 100 * 1024 * 1024,
        batch_size: int = 100,
        max_snapshots: int = 5,
        retention_hours: float = 48.0,
        max_memory_rows: int = 250_000,
    ) -> None:
        import duckdb

        self.path = Path(path)
        self.snapshot_dir = (self.path.parent / "snapshots") if self.path.suffix == ".duckdb" else self.path
        self.max_bytes = max(1, int(max_bytes))
        self.batch_size = max(1, int(batch_size))
        self.max_snapshots = max(1, int(max_snapshots))
        self.retention_hours = max(0.1, float(retention_hours))
        self.max_memory_rows = max(100, int(max_memory_rows))
        self._lock = threading.Lock()
        self._pending: list[tuple] = []
        self._dirty_since_snapshot = False
        self.last_snapshot_utc: str | None = None

        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(":memory:")
        self.conn.execute("SET TimeZone = 'UTC'")
        self.conn.execute("SET memory_limit = '256MB'")
        self.conn.execute("SET threads = 2")
        self._ensure_schema()
        self._reload_last_snapshot_locked()

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
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS fdd_results (
                ts TIMESTAMPTZ NOT NULL,
                equipment VARCHAR NOT NULL,
                rule VARCHAR NOT NULL,
                fault_flag BOOLEAN NOT NULL,
                detail VARCHAR NOT NULL
            )
        """)

    def _reload_last_snapshot_locked(self) -> None:
        snaps = sorted(self.snapshot_dir.glob("telemetry_*.parquet"), reverse=True)
        for snap in snaps:
            try:
                self.conn.execute(
                    """
                    INSERT INTO telemetry (
                        ref, timestamp_utc, site, source, device, object, property,
                        value_num, value_text, units, status
                    )
                    SELECT
                        CAST(ref AS VARCHAR),
                        CAST(timestamp_utc AS TIMESTAMPTZ),
                        CAST(site AS VARCHAR),
                        CAST(source AS VARCHAR),
                        CAST(device AS VARCHAR),
                        CAST(object AS VARCHAR),
                        CAST(property AS VARCHAR),
                        CAST(value_num AS DOUBLE),
                        CAST(value_text AS VARCHAR),
                        CAST(units AS VARCHAR),
                        CAST(status AS VARCHAR)
                    FROM read_parquet(?)
                    """,
                    [str(snap)],
                )
                mtime = datetime.fromtimestamp(snap.stat().st_mtime, tz=timezone.utc)
                self.last_snapshot_utc = mtime.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                LOG.info("Reloaded Parquet snapshot %s (%d rows)", snap.name, self.row_count_locked())
                break
            except Exception as exc:
                LOG.warning("Skipping unreadable snapshot %s: %s", snap.name, exc)

        fdd_snaps = sorted(self.snapshot_dir.glob("fdd_results_*.parquet"), reverse=True)
        for fsnap in fdd_snaps:
            try:
                self.conn.execute(
                    """
                    INSERT INTO fdd_results (ts, equipment, rule, fault_flag, detail)
                    SELECT
                        CAST(ts AS TIMESTAMPTZ),
                        CAST(equipment AS VARCHAR),
                        CAST(rule AS VARCHAR),
                        CAST(fault_flag AS BOOLEAN),
                        CAST(detail AS VARCHAR)
                    FROM read_parquet(?)
                    """,
                    [str(fsnap)],
                )
                break
            except Exception as exc:
                LOG.warning("Skipping unreadable FDD snapshot %s: %s", fsnap.name, exc)

        self._prune_memory_locked()

    def row_count_locked(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()
        return int(row[0]) if row else 0

    def row_count(self) -> int:
        with self._lock:
            return self.row_count_locked()

    def disk_bytes(self) -> int:
        if not self.snapshot_dir.exists():
            return 0
        return sum(p.stat().st_size for p in self.snapshot_dir.glob("*.parquet") if p.is_file())

    def snapshot_count(self) -> int:
        if not self.snapshot_dir.exists():
            return 0
        return len(list(self.snapshot_dir.glob("telemetry_*.parquet")))

    def _prune_memory_locked(self) -> None:
        self.conn.execute(
            """
            DELETE FROM telemetry
            WHERE timestamp_utc < CURRENT_TIMESTAMP - (CAST(? AS DOUBLE) * INTERVAL '1' HOUR)
            """,
            [self.retention_hours],
        )
        cnt = self.row_count_locked()
        if cnt > self.max_memory_rows:
            excess = cnt - self.max_memory_rows
            self.conn.execute(
                """
                DELETE FROM telemetry
                WHERE rowid IN (
                    SELECT rowid FROM telemetry ORDER BY timestamp_utc ASC LIMIT ?
                )
                """,
                [excess],
            )
        self.conn.execute(
            """
            DELETE FROM fdd_results
            WHERE ts < CURRENT_TIMESTAMP - (CAST(? AS DOUBLE) * INTERVAL '1' HOUR)
            """,
            [self.retention_hours],
        )
        fcnt = int(self.conn.execute("SELECT COUNT(*) FROM fdd_results").fetchone()[0])
        if fcnt > self.max_memory_rows:
            self.conn.execute(
                """
                DELETE FROM fdd_results
                WHERE rowid IN (
                    SELECT rowid FROM fdd_results ORDER BY ts ASC LIMIT ?
                )
                """,
                [fcnt - self.max_memory_rows],
            )

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
        self._dirty_since_snapshot = True
        self._prune_memory_locked()
        return len(batch)

    def sync_from_hot(self, hot: HotCache) -> int:
        """Ingest newer latest samples directly from HotCache into in-memory DuckDB."""
        snap = hot.snapshot_latest()
        if not snap:
            return 0
        with self._lock:
            self._flush_locked()
            cur = self.conn.execute(
                "SELECT ref, epoch_us(MAX(timestamp_utc)) FROM telemetry GROUP BY ref"
            )
            latest_by_ref = {str(r[0]): int(r[1]) for r in cur.fetchall() if r[0] and r[1] is not None}
            rows: list[tuple] = []
            for rec in snap:
                ref = str(rec.get("ref") or "")
                ts = str(rec.get("timestamp_utc") or "")
                if not ref or not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    ts_us = int(round(dt.timestamp() * 1_000_000))
                except ValueError:
                    ts_us = None
                prev_us = latest_by_ref.get(ref)
                if prev_us is not None and ts_us is not None and ts_us <= prev_us:
                    continue
                v_num, v_txt = _split_value(rec.get("value"))
                rows.append(
                    (
                        ref,
                        ts,
                        str(rec.get("site") or "demo-site"),
                        str(rec.get("source") or "hot-cache"),
                        str(rec.get("device") or ""),
                        str(rec.get("object") or ""),
                        str(rec.get("property") or "present-value"),
                        v_num,
                        v_txt,
                        str(rec.get("units") or ""),
                        str(rec.get("status") or "ok"),
                    )
                )
            if not rows:
                return 0
            self.conn.executemany(
                """
                INSERT INTO telemetry (
                    ref, timestamp_utc, site, source, device, object, property,
                    value_num, value_text, units, status
                ) VALUES (?, CAST(? AS TIMESTAMPTZ), ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._dirty_since_snapshot = True
            self._prune_memory_locked()
            return len(rows)

    def record_fdd_results(self, rows: Sequence[tuple[str, str, str, bool, str]]) -> int:
        if not rows:
            return 0
        with self._lock:
            self.conn.executemany(
                """
                INSERT INTO fdd_results (ts, equipment, rule, fault_flag, detail)
                VALUES (CAST(? AS TIMESTAMPTZ), ?, ?, ?, ?)
                """,
                list(rows),
            )
            self._dirty_since_snapshot = True
            self._prune_memory_locked()
            return len(rows)

    def _enforce_disk_cap_locked(self) -> None:
        t_snaps = sorted(self.snapshot_dir.glob("telemetry_*.parquet"))
        f_snaps = sorted(self.snapshot_dir.glob("fdd_results_*.parquet"))
        while len(t_snaps) > self.max_snapshots:
            oldest = t_snaps.pop(0)
            oldest.unlink(missing_ok=True)
        while len(f_snaps) > self.max_snapshots:
            oldest = f_snaps.pop(0)
            oldest.unlink(missing_ok=True)

        while self.disk_bytes() > self.max_bytes:
            if len(t_snaps) > 1:
                oldest = t_snaps.pop(0)
                oldest.unlink(missing_ok=True)
                continue
            if f_snaps:
                oldest = f_snaps.pop(0)
                oldest.unlink(missing_ok=True)
                continue
            break

    def write_snapshot(self, *, force: bool = False) -> Path | None:
        """Write a compact ZSTD Parquet snapshot to disk and enforce rotation/size cap."""
        with self._lock:
            self._flush_locked()
            self._prune_memory_locked()
            cnt = self.row_count_locked()
            if cnt == 0:
                return None
            existing = list(self.snapshot_dir.glob("telemetry_*.parquet"))
            if not force and not self._dirty_since_snapshot and existing:
                return None

            while True:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                tmp_t = self.snapshot_dir / f".telemetry_{stamp}.parquet.tmp"
                final_t = self.snapshot_dir / f"telemetry_{stamp}.parquet"
                self.conn.execute(
                    f"""
                    COPY (
                        SELECT
                            ref, timestamp_utc, site, source, device, object, property,
                            value_num, value_text, units, status
                        FROM telemetry
                        ORDER BY timestamp_utc ASC
                    ) TO '{_sql_quote(tmp_t)}' (FORMAT PARQUET, COMPRESSION ZSTD)
                    """
                )
                os.replace(tmp_t, final_t)

                fcnt = int(self.conn.execute("SELECT COUNT(*) FROM fdd_results").fetchone()[0])
                if fcnt > 0:
                    tmp_f = self.snapshot_dir / f".fdd_results_{stamp}.parquet.tmp"
                    final_f = self.snapshot_dir / f"fdd_results_{stamp}.parquet"
                    self.conn.execute(
                        f"""
                        COPY (
                            SELECT ts, equipment, rule, fault_flag, detail
                            FROM fdd_results
                            ORDER BY ts ASC
                        ) TO '{_sql_quote(tmp_f)}' (FORMAT PARQUET, COMPRESSION ZSTD)
                        """
                    )
                    os.replace(tmp_f, final_f)

                self._enforce_disk_cap_locked()
                if self.disk_bytes() <= self.max_bytes or self.row_count_locked() == 0:
                    break
                # Single snapshot exceeded cap: trim oldest 25% of in-memory rows and rewrite.
                cur_cnt = self.row_count_locked()
                trim = max(1, cur_cnt // 4)
                self.conn.execute(
                    """
                    DELETE FROM telemetry
                    WHERE rowid IN (
                        SELECT rowid FROM telemetry ORDER BY timestamp_utc ASC LIMIT ?
                    )
                    """,
                    [trim],
                )
                if self.row_count_locked() == 0:
                    final_t.unlink(missing_ok=True)
                    break

            self._dirty_since_snapshot = False
            self.last_snapshot_utc = utc_now_iso()
            return final_t

    def export_bundle_zip(
        self,
        targets: Sequence[PointTarget],
        *,
        hours: float = 24.0,
        graph: Any = None,
    ) -> bytes:
        """Export samples, fdd_results, and points Parquet files in a single ZIP."""
        hours = max(0.01, min(float(hours), 720.0))
        target_by_ref: dict[str, PointTarget] = {}
        for t in targets:
            if t.ref:
                target_by_ref[t.ref] = t

        with self._lock:
            self._flush_locked()
            raw_samples = self.conn.execute(
                """
                SELECT timestamp_utc, ref, value_num, units
                FROM telemetry
                WHERE timestamp_utc >= CURRENT_TIMESTAMP - (CAST(? AS DOUBLE) * INTERVAL '1' HOUR)
                ORDER BY timestamp_utc ASC
                """,
                [hours],
            ).fetchall()

            sample_rows: list[tuple] = []
            seen_refs: set[str] = set()
            for ts_val, ref, val_num, raw_unit in raw_samples:
                seen_refs.add(ref)
                t = target_by_ref.get(ref)
                eff_unit = raw_unit or (t.units if t else "")
                si_val, si_unit = to_si(float(val_num) if val_num is not None else None, eff_unit)
                sample_rows.append((ts_val, ref, si_val, si_unit))

            point_rows: list[tuple] = []
            for t in targets:
                if not t.ref:
                    continue
                point_rows.append(
                    (
                        t.ref,
                        _target_equipment(t, t.ref),
                        _target_haystack_tags(t),
                        _target_223p_class(t, graph=graph),
                        normalize_si_unit(t.units),
                    )
                )
            for ref in sorted(seen_refs - set(target_by_ref)):
                point_rows.append((ref, _target_equipment(None, ref), "", "", ""))

            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                samples_pq = tmp / "samples.parquet"
                fdd_pq = tmp / "fdd_results.parquet"
                points_pq = tmp / "points.parquet"

                self.conn.execute("DROP TABLE IF EXISTS _export_samples")
                self.conn.execute(
                    "CREATE TEMP TABLE _export_samples (ts TIMESTAMPTZ, point_id VARCHAR, value DOUBLE, units VARCHAR)"
                )
                if sample_rows:
                    self.conn.executemany(
                        "INSERT INTO _export_samples VALUES (?, ?, ?, ?)",
                        sample_rows,
                    )
                self.conn.execute(
                    f"COPY _export_samples TO '{_sql_quote(samples_pq)}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                self.conn.execute("DROP TABLE IF EXISTS _export_samples")

                self.conn.execute("DROP TABLE IF EXISTS _export_fdd")
                self.conn.execute(
                    """
                    CREATE TEMP TABLE _export_fdd AS
                    SELECT ts, equipment, rule, fault_flag, detail
                    FROM fdd_results
                    WHERE ts >= CURRENT_TIMESTAMP - (CAST(? AS DOUBLE) * INTERVAL '1' HOUR)
                    ORDER BY ts ASC
                    """,
                    [hours],
                )
                self.conn.execute(
                    f"COPY _export_fdd TO '{_sql_quote(fdd_pq)}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                self.conn.execute("DROP TABLE IF EXISTS _export_fdd")

                self.conn.execute("DROP TABLE IF EXISTS _export_points")
                self.conn.execute(
                    """
                    CREATE TEMP TABLE _export_points (
                        point_id VARCHAR,
                        equipment VARCHAR,
                        haystack_tags VARCHAR,
                        class_223p VARCHAR,
                        unit VARCHAR
                    )
                    """
                )
                if point_rows:
                    self.conn.executemany(
                        "INSERT INTO _export_points VALUES (?, ?, ?, ?, ?)",
                        point_rows,
                    )
                self.conn.execute(
                    f"COPY _export_points TO '{_sql_quote(points_pq)}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                self.conn.execute("DROP TABLE IF EXISTS _export_points")

                buf = io.BytesIO()
                with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(samples_pq, arcname="samples.parquet")
                    zf.write(fdd_pq, arcname="fdd_results.parquet")
                    zf.write(points_pq, arcname="points.parquet")
                return buf.getvalue()

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

    def run_sql_file(self, path: Path, params: Sequence[Any]) -> list[dict[str, Any]]:
        sql = path.read_text(encoding="utf-8")
        with self._lock:
            cur = self.conn.execute(sql, list(params))
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def close(self) -> None:
        try:
            self.write_snapshot()
        except Exception:
            LOG.exception("snapshot on close failed")
        with self._lock:
            self._flush_locked()
            self.conn.close()
