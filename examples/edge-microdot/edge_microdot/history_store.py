"""In-memory DuckDB history store with Open-FDD core schema and Parquet snapshots."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Sequence

from edge_microdot.cache import HotCache, _split_value
from edge_microdot.models import PointSample, PointTarget, utc_now_iso
from edge_microdot.role_terms import input_term_from_target
from edge_microdot.units_si import normalize_si_unit, to_si

LOG = logging.getLogger(__name__)


def _json_cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# Open-FDD fdd.input_sample / core.sql column contracts.
INPUT_SAMPLE_COLUMNS = ("site_id", "point_key", "ts", "value")
POINT_COLUMNS = (
    "site_id",
    "point_key",
    "equipment_id",
    "role",
    "role_source",
    "kind",
    "unit",
    "source_unit",
    "si_scale",
    "si_offset",
    "haystack_id",
    "attrs",
    "updated_at",
)
RULE_RESULT_COLUMNS = (
    "result_id",
    "site_id",
    "equipment_id",
    "rule_id",
    "subject_key",
    "window_start",
    "window_end",
    "rule_hash",
    "config_hash",
    "config_revision",
    "effective_parameters",
    "poll_seconds",
    "status",
    "reason",
    "fault_hours",
    "covered_hours",
    "fault_pct",
    "role",
    "engine",
    "metrics",
    "diagnostics",
    "source_watermark",
    "created_at",
)
RULE_RESULT_STATUSES = frozenset(
    {"PASS", "FAULT", "INCONCLUSIVE", "SKIPPED", "ERROR", "STALE", "METRIC"}
)
BUNDLE_FILES = ("input_sample.parquet", "point.parquet", "rule_result.parquet")


def _sql_quote(path: Path) -> str:
    return str(path).replace("'", "''")


_GEN_DIR = re.compile(r"^gen_(\d+)$")
_FLAT_SAMPLE = re.compile(r"^(?:sample_store|input_sample)_(.+)\.parquet$")
_FLAT_POINT = re.compile(r"^point_(.+)\.parquet$")
_FLAT_RULE = re.compile(r"^rule_result_(.+)\.parquet$")


class _SnapshotGeneration(NamedTuple):
    key: tuple
    sample: Path
    point: Path
    rule: Path | None
    manifest: Path | None
    directory: Path | None

    def files(self) -> list[Path]:
        found = [self.sample, self.point]
        if self.rule is not None and self.rule.is_file():
            found.append(self.rule)
        if self.manifest is not None and self.manifest.is_file():
            found.append(self.manifest)
        return found

    def nbytes(self) -> int:
        total = 0
        for path in self.files():
            if path.is_file():
                total += path.stat().st_size
        return total


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ntp_synced() -> bool:
    flag = Path("/run/systemd/timesync/synchronized")
    if flag.is_file():
        return True
    try:
        out = subprocess.check_output(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip().lower() == "yes"


def _parse_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


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


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


class HistoryStore:
    """In-memory DuckDB Open-FDD core store backed by capped Parquet snapshots."""

    def __init__(
        self,
        path: str | Path = "var/snapshots",
        *,
        max_bytes: int = 100 * 1024 * 1024,
        batch_size: int = 100,
        max_snapshots: int = 5,
        retention_hours: float = 48.0,
        max_memory_rows: int = 250_000,
        memory_limit_mb: int = 256,
    ) -> None:
        import duckdb

        self.path = Path(path)
        self.snapshot_dir = (self.path.parent / "snapshots") if self.path.suffix == ".duckdb" else self.path
        self.max_bytes = max(1, int(max_bytes))
        self.batch_size = max(1, int(batch_size))
        self.max_snapshots = max(1, int(max_snapshots))
        self.retention_hours = max(0.1, float(retention_hours))
        self.max_memory_rows = max(100, int(max_memory_rows))
        self.memory_limit_mb = max(32, min(int(memory_limit_mb), 4096))
        self._lock = threading.Lock()
        self._pending: list[dict[str, Any]] = []
        self._dirty_since_snapshot = False
        self.last_snapshot_utc: str | None = None

        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._clock_sane = datetime.now(timezone.utc).year >= 2024 or _ntp_synced()
        self._cleanup_tmp_locked()
        self.conn = duckdb.connect(":memory:")
        self.conn.execute("SET TimeZone = 'UTC'")
        self.conn.execute(f"SET memory_limit = '{self.memory_limit_mb}MB'")
        self.conn.execute("SET threads = 2")
        self.conn.execute("SET autoinstall_known_extensions = false")
        self.conn.execute("SET autoload_known_extensions = false")
        self._ensure_schema()
        self._reload_last_snapshot_locked()

    def _ensure_schema(self) -> None:
        self.conn.execute("CREATE SCHEMA IF NOT EXISTS fdd")
        self.conn.execute("CREATE SEQUENCE IF NOT EXISTS fdd.rule_result_id_seq")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fdd.point (
              site_id      TEXT NOT NULL,
              point_key    TEXT NOT NULL,
              equipment_id TEXT,
              role         TEXT,
              role_source  TEXT,
              kind         TEXT NOT NULL DEFAULT 'numeric'
                           CHECK (kind IN ('numeric','bool','text')),
              unit         TEXT,
              source_unit  TEXT,
              si_scale     DOUBLE NOT NULL DEFAULT 1
                           CHECK (si_scale > 0 AND si_scale < 'Infinity'),
              si_offset    DOUBLE NOT NULL DEFAULT 0
                           CHECK (abs(si_offset) < 'Infinity'),
              haystack_id  TEXT,
              attrs        JSON NOT NULL DEFAULT '{}',
              updated_at   TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
              PRIMARY KEY (site_id, point_key)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS point_equipment_role_idx "
            "ON fdd.point (site_id, equipment_id, role)"
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fdd.sample_store (
              site_id    TEXT NOT NULL,
              point_key  TEXT NOT NULL,
              ts         TIMESTAMPTZ NOT NULL,
              value      DOUBLE,
              value_text TEXT,
              status     TEXT,
              PRIMARY KEY (site_id, point_key, ts)
            )
            """
        )
        self.conn.execute(
            """
            CREATE OR REPLACE VIEW fdd.input_sample AS
            SELECT site_id, point_key, ts, value
            FROM fdd.sample_store
            WHERE value IS NOT NULL
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fdd.rule_result (
              result_id            BIGINT DEFAULT nextval('fdd.rule_result_id_seq'),
              site_id              TEXT NOT NULL,
              equipment_id         TEXT NOT NULL,
              rule_id              TEXT NOT NULL,
              subject_key          TEXT NOT NULL DEFAULT '',
              window_start         TIMESTAMPTZ NOT NULL,
              window_end           TIMESTAMPTZ NOT NULL
                                   CHECK (window_end > window_start),
              rule_hash            TEXT NOT NULL DEFAULT '',
              config_hash          TEXT NOT NULL DEFAULT '',
              config_revision      BIGINT,
              effective_parameters JSON NOT NULL DEFAULT '{}',
              poll_seconds         DOUBLE NOT NULL DEFAULT 300
                                   CHECK (poll_seconds >= 1 AND poll_seconds < 'Infinity'),
              status               TEXT NOT NULL
                                   CHECK (status IN ('PASS','FAULT','INCONCLUSIVE',
                                                     'SKIPPED','ERROR','STALE','METRIC')),
              reason               TEXT,
              fault_hours          DOUBLE,
              covered_hours        DOUBLE,
              fault_pct            DOUBLE,
              role                 TEXT,
              engine               TEXT,
              metrics              JSON NOT NULL DEFAULT '{}',
              diagnostics          JSON NOT NULL DEFAULT '{}',
              source_watermark     TIMESTAMPTZ,
              created_at           TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
              PRIMARY KEY (site_id, equipment_id, rule_id, subject_key,
                           window_start, window_end, rule_hash, config_hash)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS rule_result_site_rule_idx "
            "ON fdd.rule_result (site_id, rule_id, window_end DESC)"
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fdd.rule_config (
              site_id    TEXT NOT NULL,
              rule_id    TEXT NOT NULL,
              scope_key  TEXT NOT NULL,
              revision   BIGINT NOT NULL,
              parameters JSON NOT NULL,
              active     BOOLEAN NOT NULL DEFAULT false,
              created_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
              created_by TEXT NOT NULL,
              PRIMARY KEY (site_id, rule_id, scope_key, revision)
            )
            """
        )
        # Convenience views used by SQL rules and the /history API.
        self.conn.execute(
            """
            CREATE OR REPLACE VIEW sensor_history AS
            SELECT
                p.equipment_id,
                p.point_key AS series_id,
                p.role AS sensor_role,
                s.ts AS timestamp_utc,
                s.value,
                p.unit
            FROM fdd.input_sample s
            JOIN fdd.point p
              ON p.site_id = s.site_id AND p.point_key = s.point_key
            """
        )
        self.conn.execute(
            """
            CREATE OR REPLACE VIEW telemetry AS
            SELECT
                s.point_key AS ref,
                s.ts AS timestamp_utc,
                s.site_id AS site,
                COALESCE(json_extract_string(p.attrs, '$.source'), '') AS source,
                COALESCE(json_extract_string(p.attrs, '$.device'), '') AS device,
                COALESCE(json_extract_string(p.attrs, '$.object'), '') AS object,
                COALESCE(json_extract_string(p.attrs, '$.property'), 'present-value') AS property,
                s.value AS value_num,
                s.value_text,
                p.unit AS units,
                COALESCE(s.status, 'ok') AS status
            FROM fdd.sample_store s
            LEFT JOIN fdd.point p
              ON p.site_id = s.site_id AND p.point_key = s.point_key
            """
        )

    def _cleanup_tmp_locked(self) -> None:
        if not self.snapshot_dir.exists():
            return
        for path in list(self.snapshot_dir.iterdir()):
            if path.is_dir() and (path.name.endswith(".tmp") or path.name.startswith(".")):
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file() and (path.name.endswith(".tmp") or path.name.startswith(".")):
                path.unlink(missing_ok=True)

    def _generations(self) -> list[_SnapshotGeneration]:
        found: list[_SnapshotGeneration] = []
        if not self.snapshot_dir.exists():
            return found
        for path in self.snapshot_dir.iterdir():
            match = _GEN_DIR.fullmatch(path.name)
            if not match or not path.is_dir():
                continue
            manifest = path / "manifest.json"
            sample = path / "sample_store.parquet"
            point = path / "point.parquet"
            if not (manifest.is_file() and sample.is_file() and point.is_file()):
                continue
            rule = path / "rule_result.parquet"
            found.append(
                _SnapshotGeneration(
                    key=(1, int(match.group(1))),
                    sample=sample,
                    point=point,
                    rule=rule if rule.is_file() else None,
                    manifest=manifest,
                    directory=path,
                )
            )
        samples: dict[str, Path] = {}
        points: dict[str, Path] = {}
        rules: dict[str, Path] = {}
        for path in self.snapshot_dir.glob("*.parquet"):
            if not path.is_file():
                continue
            sample_m = _FLAT_SAMPLE.fullmatch(path.name)
            point_m = _FLAT_POINT.fullmatch(path.name)
            rule_m = _FLAT_RULE.fullmatch(path.name)
            if sample_m and sample_m.group(1) not in samples:
                samples[sample_m.group(1)] = path
            elif point_m:
                points[point_m.group(1)] = path
            elif rule_m:
                rules[rule_m.group(1)] = path
        for stamp in sorted(set(samples) & set(points)):
            found.append(
                _SnapshotGeneration(
                    key=(0, stamp),
                    sample=samples[stamp],
                    point=points[stamp],
                    rule=rules.get(stamp),
                    manifest=None,
                    directory=None,
                )
            )
        found.sort(key=lambda item: item.key)
        return found

    def _delete_legacy_files(self) -> None:
        for pattern in ("telemetry_*.parquet", "fdd_results_*.parquet"):
            for path in self.snapshot_dir.glob(pattern):
                if path.is_file():
                    path.unlink(missing_ok=True)

    def _remove_generation(self, gen: _SnapshotGeneration) -> None:
        if gen.directory is not None:
            resolved = gen.directory.resolve()
            root = self.snapshot_dir.resolve()
            if resolved.parent == root and _GEN_DIR.fullmatch(resolved.name):
                shutil.rmtree(resolved, ignore_errors=True)
                return
        for path in gen.files():
            if path.is_file():
                path.unlink(missing_ok=True)

    def _reseed_rule_result_seq(self) -> None:
        row = self.conn.execute("SELECT MAX(result_id) FROM fdd.rule_result").fetchone()
        if not row or row[0] is None:
            return
        self.conn.execute("SELECT setval('fdd.rule_result_id_seq', ?)", [int(row[0])])

    def _manifest_written_utc(self, gen: _SnapshotGeneration) -> datetime | None:
        if gen.manifest is None or not gen.manifest.is_file():
            return None
        try:
            body = json.loads(gen.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return _parse_utc(body.get("written_utc") if isinstance(body, dict) else None)

    def _clock_allows_snapshot(self, now: datetime) -> bool:
        gens = self._generations()
        if gens:
            written = self._manifest_written_utc(gens[-1])
            if written is not None and now < written:
                LOG.warning("skipping snapshot; clock %s is behind last generation %s", now.isoformat(), written.isoformat())
                return False
        if now.year < 2024 and not _ntp_synced():
            LOG.warning("skipping snapshot; clock is not known to be synced")
            return False
        return True

    def _reload_last_snapshot_locked(self) -> None:
        if self._reload_core_snapshots():
            self._prune_memory_locked()
        else:
            self._reload_legacy_snapshots()
            self._prune_memory_locked()
        self._reseed_rule_result_seq()

    def _reload_core_snapshots(self) -> bool:
        generations = self._generations()
        if not generations:
            return False
        for gen in reversed(generations):
            if self._load_generation(gen):
                self._delete_legacy_files()
                return True
        return False

    def _load_generation(self, gen: _SnapshotGeneration) -> bool:
        sample_snaps = [gen.sample]
        loaded = False
        for snap in sample_snaps:
            try:
                cols = [
                    r[0]
                    for r in self.conn.execute(
                        "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?))",
                        [str(snap)],
                    ).fetchall()
                ]
                colset = set(cols)
                if {"site_id", "point_key", "ts", "value"} <= colset:
                    has_text = "value_text" in colset
                    has_status = "status" in colset
                    self.conn.execute(
                        f"""
                        INSERT INTO fdd.sample_store
                            (site_id, point_key, ts, value, value_text, status)
                        SELECT
                            CAST(site_id AS VARCHAR),
                            CAST(point_key AS VARCHAR),
                            CAST(ts AS TIMESTAMPTZ),
                            CAST(value AS DOUBLE),
                            {'CAST(value_text AS VARCHAR)' if has_text else 'NULL'},
                            {'CAST(status AS VARCHAR)' if has_status else 'NULL'}
                        FROM read_parquet(?)
                        ON CONFLICT (site_id, point_key, ts) DO NOTHING
                        """,
                        [str(snap)],
                    )
                elif {"ref", "timestamp_utc"} <= colset:
                    # Should have been handled by legacy; skip here.
                    continue
                else:
                    continue
                mtime = datetime.fromtimestamp(snap.stat().st_mtime, tz=timezone.utc)
                self.last_snapshot_utc = mtime.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                LOG.info("Reloaded Parquet snapshot %s (%d rows)", snap.name, self.row_count_locked())
                loaded = True
                break
            except Exception as exc:
                LOG.warning("Skipping unreadable snapshot %s: %s", snap.name, exc)

        point_snaps = [gen.point]
        for snap in point_snaps:
            try:
                self.conn.execute(
                    """
                    INSERT INTO fdd.point (
                        site_id, point_key, equipment_id, role, role_source, kind,
                        unit, source_unit, si_scale, si_offset, haystack_id, attrs, updated_at
                    )
                    SELECT
                        CAST(site_id AS VARCHAR),
                        CAST(point_key AS VARCHAR),
                        CAST(equipment_id AS VARCHAR),
                        CAST(role AS VARCHAR),
                        CAST(role_source AS VARCHAR),
                        COALESCE(CAST(kind AS VARCHAR), 'numeric'),
                        CAST(unit AS VARCHAR),
                        CAST(source_unit AS VARCHAR),
                        COALESCE(CAST(si_scale AS DOUBLE), 1),
                        COALESCE(CAST(si_offset AS DOUBLE), 0),
                        CAST(haystack_id AS VARCHAR),
                        COALESCE(attrs, '{}'::JSON),
                        COALESCE(CAST(updated_at AS TIMESTAMPTZ), CURRENT_TIMESTAMP)
                    FROM read_parquet(?)
                    ON CONFLICT (site_id, point_key) DO NOTHING
                    """,
                    [str(snap)],
                )
                break
            except Exception as exc:
                LOG.warning("Skipping unreadable point snapshot %s: %s", snap.name, exc)

        result_snaps = [gen.rule] if gen.rule is not None else []
        for snap in result_snaps:
            try:
                self.conn.execute(
                    """
                    INSERT INTO fdd.rule_result (
                        result_id, site_id, equipment_id, rule_id, subject_key,
                        window_start, window_end, rule_hash, config_hash, config_revision,
                        effective_parameters, poll_seconds, status, reason,
                        fault_hours, covered_hours, fault_pct, role, engine,
                        metrics, diagnostics, source_watermark, created_at
                    )
                    SELECT
                        result_id,
                        CAST(site_id AS VARCHAR),
                        CAST(equipment_id AS VARCHAR),
                        CAST(rule_id AS VARCHAR),
                        COALESCE(CAST(subject_key AS VARCHAR), ''),
                        CAST(window_start AS TIMESTAMPTZ),
                        CAST(window_end AS TIMESTAMPTZ),
                        COALESCE(CAST(rule_hash AS VARCHAR), ''),
                        COALESCE(CAST(config_hash AS VARCHAR), ''),
                        config_revision,
                        COALESCE(effective_parameters, '{}'::JSON),
                        COALESCE(CAST(poll_seconds AS DOUBLE), 300),
                        CAST(status AS VARCHAR),
                        CAST(reason AS VARCHAR),
                        CAST(fault_hours AS DOUBLE),
                        CAST(covered_hours AS DOUBLE),
                        CAST(fault_pct AS DOUBLE),
                        CAST(role AS VARCHAR),
                        CAST(engine AS VARCHAR),
                        COALESCE(metrics, '{}'::JSON),
                        COALESCE(diagnostics, '{}'::JSON),
                        CAST(source_watermark AS TIMESTAMPTZ),
                        COALESCE(CAST(created_at AS TIMESTAMPTZ), CURRENT_TIMESTAMP)
                    FROM read_parquet(?)
                    ON CONFLICT DO NOTHING
                    """,
                    [str(snap)],
                )
                break
            except Exception as exc:
                LOG.warning("Skipping unreadable rule_result snapshot %s: %s", snap.name, exc)
        return loaded

    def _reload_legacy_snapshots(self) -> None:
        """Map pre-parity telemetry_ / fdd_results_ snapshots into the core schema.

        Uses set-based INSERT...SELECT so a large rolling window (100k+ rows)
        cannot stall HTTP startup the way a per-row Python loop would.
        """
        snaps = sorted(self.snapshot_dir.glob("telemetry_*.parquet"), reverse=True)
        for snap in snaps:
            try:
                n = int(self.conn.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(snap)]).fetchone()[0])
                LOG.info("Reloading legacy Parquet snapshot %s (%d rows) in bulk", snap.name, n)
                self.conn.execute("DROP TABLE IF EXISTS _legacy_tel")
                self.conn.execute(
                    """
                    CREATE TEMP TABLE _legacy_tel AS
                    SELECT
                        CAST(ref AS VARCHAR) AS ref,
                        CAST(timestamp_utc AS TIMESTAMPTZ) AS timestamp_utc,
                        CAST(site AS VARCHAR) AS site,
                        CAST(source AS VARCHAR) AS source,
                        CAST(device AS VARCHAR) AS device,
                        CAST(object AS VARCHAR) AS object,
                        CAST(property AS VARCHAR) AS property,
                        CAST(value_num AS DOUBLE) AS value_num,
                        CAST(value_text AS VARCHAR) AS value_text,
                        CAST(units AS VARCHAR) AS units,
                        CAST(status AS VARCHAR) AS status,
                        lower(replace(replace(replace(coalesce(CAST(units AS VARCHAR), ''),
                              chr(176), ''), ' ', ''), '_', '')) AS unit_key
                    FROM read_parquet(?)
                    """,
                    [str(snap)],
                )
                self.conn.execute(
                    """
                    CREATE OR REPLACE TEMP VIEW _legacy_si AS
                    SELECT
                        coalesce(site, '') AS site_id,
                        ref AS point_key,
                        timestamp_utc AS ts,
                        value_text,
                        status,
                        source, device, object, property, units,
                        CASE
                            WHEN unit_key IN ('f', 'degf', 'degreef', 'fahrenheit')
                                THEN (value_num - 32.0) * 5.0 / 9.0
                            WHEN unit_key IN ('inh2o', 'inwc') THEN value_num * 249.08891
                            WHEN unit_key = 'psi' THEN value_num * 6894.757293
                            WHEN unit_key = 'kpa' THEN value_num * 1000.0
                            WHEN unit_key IN ('cfm', 'ft3/min', 'ft3-per-min')
                                THEN value_num * 0.00047194745
                            WHEN unit_key = 'gpm' THEN value_num * 6.30901964e-5
                            WHEN unit_key = 'kw' THEN value_num * 1000.0
                            WHEN unit_key = 'kwh' THEN value_num * 3600000.0
                            ELSE value_num
                        END AS value,
                        CASE
                            WHEN unit_key IN ('f', 'degf', 'degreef', 'fahrenheit',
                                              'c', 'cel', 'degc', 'degreec', 'degreecelsius')
                                THEN 'degC'
                            WHEN unit_key IN ('k', 'degk') THEN 'K'
                            WHEN unit_key IN ('inh2o', 'inwc', 'psi', 'kpa', 'pa') THEN 'Pa'
                            WHEN unit_key IN ('cfm', 'ft3/min', 'ft3-per-min', 'gpm',
                                              'm3/s', 'm3-per-sec') THEN 'm3/s'
                            WHEN unit_key IN ('kw', 'w') THEN 'W'
                            WHEN unit_key = 'kwh' THEN 'J'
                            WHEN unit_key = 'hz' THEN 'Hz'
                            WHEN unit_key IN ('%', 'percent') THEN '%'
                            WHEN unit_key IN ('%rh', 'percentrh') THEN '%RH'
                            WHEN unit_key = 'ppm' THEN 'ppm'
                            ELSE units
                        END AS si_unit
                    FROM _legacy_tel
                    """
                )
                self.conn.execute(
                    """
                    INSERT INTO fdd.point (
                        site_id, point_key, equipment_id, role, unit, source_unit, attrs, updated_at
                    )
                    SELECT
                        site_id,
                        point_key,
                        CASE
                            WHEN coalesce(device, '') <> '' THEN 'device-' || device
                            WHEN point_key LIKE 'bacnet://%' THEN
                                'device-' || split_part(substr(point_key, 10), '/', 1)
                            WHEN point_key LIKE 'niagara://%' THEN 'niagara'
                            ELSE 'edge'
                        END,
                        '',
                        any_value(si_unit),
                        any_value(units),
                        json_object(
                            'source', coalesce(any_value(source), ''),
                            'device', coalesce(any_value(device), ''),
                            'object', coalesce(any_value(object), ''),
                            'property', coalesce(any_value(property), 'present-value'),
                            'legacy_snapshot', true
                        ),
                        now()
                    FROM _legacy_si
                    GROUP BY site_id, point_key, device
                    ON CONFLICT (site_id, point_key) DO NOTHING
                    """
                )
                self.conn.execute(
                    """
                    INSERT INTO fdd.sample_store
                        (site_id, point_key, ts, value, value_text, status)
                    SELECT site_id, point_key, ts, value, value_text, status
                    FROM _legacy_si
                    ON CONFLICT (site_id, point_key, ts) DO NOTHING
                    """
                )
                self.conn.execute("DROP VIEW IF EXISTS _legacy_si")
                self.conn.execute("DROP TABLE IF EXISTS _legacy_tel")
                mtime = datetime.fromtimestamp(snap.stat().st_mtime, tz=timezone.utc)
                self.last_snapshot_utc = mtime.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                LOG.info("Reloaded legacy Parquet snapshot %s (%d rows)", snap.name, self.row_count_locked())
                break
            except Exception as exc:
                LOG.warning("Skipping unreadable legacy snapshot %s: %s", snap.name, exc)

        fdd_snaps = sorted(self.snapshot_dir.glob("fdd_results_*.parquet"), reverse=True)
        for fsnap in fdd_snaps:
            try:
                self.conn.execute(
                    """
                    INSERT INTO fdd.rule_result (
                        site_id, equipment_id, rule_id, subject_key,
                        window_start, window_end, status, reason,
                        fault_hours, covered_hours, engine
                    )
                    SELECT
                        '',
                        coalesce(CAST(equipment AS VARCHAR), 'edge'),
                        coalesce(CAST(rule AS VARCHAR), 'UNKNOWN'),
                        '',
                        CAST(ts AS TIMESTAMPTZ) - INTERVAL 1 HOUR,
                        CAST(ts AS TIMESTAMPTZ),
                        CASE WHEN CAST(fault_flag AS BOOLEAN) THEN 'FAULT' ELSE 'PASS' END,
                        CAST(detail AS VARCHAR),
                        CASE WHEN CAST(fault_flag AS BOOLEAN) THEN 1.0 ELSE 0.0 END,
                        1.0,
                        'duckdb'
                    FROM read_parquet(?)
                    WHERE CAST(ts AS TIMESTAMPTZ) IS NOT NULL
                    ON CONFLICT DO NOTHING
                    """,
                    [str(fsnap)],
                )
                break
            except Exception as exc:
                LOG.warning("Skipping unreadable legacy FDD snapshot %s: %s", fsnap.name, exc)

    def _upsert_point_row(
        self,
        *,
        site_id: str,
        point_key: str,
        equipment_id: str | None,
        role: str,
        unit: str,
        source_unit: str,
        attrs: dict[str, Any],
        role_source: str | None = None,
        haystack_id: str | None = None,
        kind: str = "numeric",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO fdd.point (
                site_id, point_key, equipment_id, role, role_source, kind,
                unit, source_unit, haystack_id, attrs, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(? AS JSON), now())
            ON CONFLICT (site_id, point_key) DO UPDATE SET
                equipment_id = COALESCE(excluded.equipment_id, fdd.point.equipment_id),
                role = CASE
                    WHEN excluded.role IS NOT NULL AND excluded.role <> ''
                    THEN excluded.role ELSE fdd.point.role END,
                role_source = COALESCE(excluded.role_source, fdd.point.role_source),
                kind = excluded.kind,
                unit = COALESCE(excluded.unit, fdd.point.unit),
                source_unit = COALESCE(excluded.source_unit, fdd.point.source_unit),
                haystack_id = COALESCE(excluded.haystack_id, fdd.point.haystack_id),
                attrs = json_merge_patch(COALESCE(fdd.point.attrs, '{}'::JSON), excluded.attrs),
                updated_at = now()
            """,
            [
                site_id,
                point_key,
                equipment_id,
                role or "",
                role_source,
                kind,
                unit or None,
                source_unit or None,
                haystack_id,
                _json_dumps(attrs),
            ],
        )

    def upsert_points(self, targets: Sequence[PointTarget], graph: Any = None) -> int:
        """Bind PointTarget metadata into fdd.point (223P snake_case roles, SI units)."""
        n = 0
        with self._lock:
            for t in targets:
                if not t.ref:
                    continue
                attrs = {
                    "source": t.source if hasattr(t, "source") else "",
                    "device": t.device or "",
                    "object": t.object_id or "",
                    "property": t.property_id or "present-value",
                    "haystack_tags": _target_haystack_tags(t),
                    "class_223p": _target_223p_class(t, graph=graph),
                    "label": t.label or "",
                }
                self._upsert_point_row(
                    site_id=t.site or "",
                    point_key=t.ref,
                    equipment_id=_target_equipment(t, t.ref),
                    role=input_term_from_target(t),
                    unit=normalize_si_unit(t.units or ""),
                    source_unit=t.units or "",
                    attrs=attrs,
                    role_source="223p_snake_case" if input_term_from_target(t) else None,
                    haystack_id=None,
                )
                n += 1
            if n:
                self._dirty_since_snapshot = True
        return n

    def row_count_locked(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM fdd.sample_store").fetchone()
        return int(row[0]) if row else 0

    def row_count(self) -> int:
        with self._lock:
            return self.row_count_locked()

    def disk_bytes(self) -> int:
        return sum(gen.nbytes() for gen in self._generations())

    def snapshot_count(self) -> int:
        gens = self._generations()
        if gens:
            return len(gens)
        if not self.snapshot_dir.exists():
            return 0
        return len(list(self.snapshot_dir.glob("telemetry_*.parquet")))

    def _prune_memory_locked(self) -> None:
        self.conn.execute(
            """
            DELETE FROM fdd.sample_store
            WHERE ts < CURRENT_TIMESTAMP - (CAST(? AS DOUBLE) * INTERVAL '1' HOUR)
            """,
            [self.retention_hours],
        )
        cnt = self.row_count_locked()
        if cnt > self.max_memory_rows:
            excess = cnt - self.max_memory_rows
            self.conn.execute(
                """
                DELETE FROM fdd.sample_store
                WHERE (site_id, point_key, ts) IN (
                    SELECT site_id, point_key, ts
                    FROM fdd.sample_store
                    ORDER BY ts ASC
                    LIMIT ?
                )
                """,
                [excess],
            )
        self.conn.execute(
            """
            DELETE FROM fdd.rule_result
            WHERE window_end < CURRENT_TIMESTAMP - (CAST(? AS DOUBLE) * INTERVAL '1' HOUR)
            """,
            [self.retention_hours],
        )
        fcnt = int(self.conn.execute("SELECT COUNT(*) FROM fdd.rule_result").fetchone()[0])
        if fcnt > self.max_memory_rows:
            self.conn.execute(
                """
                DELETE FROM fdd.rule_result
                WHERE (site_id, equipment_id, rule_id, subject_key, window_start, window_end, rule_hash, config_hash)
                IN (
                    SELECT site_id, equipment_id, rule_id, subject_key, window_start, window_end, rule_hash, config_hash
                    FROM fdd.rule_result
                    ORDER BY window_end ASC
                    LIMIT ?
                )
                """,
                [fcnt - self.max_memory_rows],
            )

    def stage(self, samples: Sequence[PointSample]) -> None:
        if not samples:
            return
        with self._lock:
            for s in samples:
                self._pending.append(self._sample_to_pending(s))
            if len(self._pending) >= self.batch_size:
                self._flush_locked()

    def _sample_to_pending(self, s: PointSample) -> dict[str, Any]:
        v_num, v_txt = _split_value(s.value)
        ref = s.ref or f"bacnet://{s.device}/{s.object}"
        si_val, si_unit = to_si(v_num, s.units or "")
        return {
            "site_id": s.site or "",
            "point_key": ref,
            "ts": s.timestamp_utc,
            "value": si_val,
            "value_text": v_txt if si_val is None else (None if v_txt is None else str(si_val)),
            "status": "clock_unsynced" if not self._clock_sane and (not s.status or s.status == "ok") else s.status,
            "equipment_id": _target_equipment(None, ref),
            "unit": si_unit or normalize_si_unit(s.units or ""),
            "source_unit": s.units or "",
            "attrs": {
                "source": s.source or "",
                "device": s.device or "",
                "object": s.object or "",
                "property": s.property or "present-value",
            },
        }

    def flush(self) -> int:
        with self._lock:
            return self._flush_locked()

    def prune_memory(self) -> None:
        """Apply retention and the in-memory row cap. Called on a timer, not per row."""
        with self._lock:
            self._prune_memory_locked()

    def _flush_locked(self) -> int:
        if not self._pending:
            return 0
        batch = self._pending
        self._pending = []
        try:
            self.conn.execute("BEGIN")
            self.conn.executemany(
                """
                INSERT INTO fdd.point (
                    site_id, point_key, equipment_id, role, kind, unit, source_unit, attrs
                ) VALUES (?, ?, ?, '', 'numeric', ?, ?, CAST(? AS JSON))
                ON CONFLICT (site_id, point_key) DO NOTHING
                """,
                [
                    [
                        rec["site_id"],
                        rec["point_key"],
                        rec["equipment_id"],
                        rec["unit"] or None,
                        rec["source_unit"] or None,
                        _json_dumps(rec["attrs"]),
                    ]
                    for rec in batch
                ],
            )
            self.conn.executemany(
                """
                INSERT INTO fdd.sample_store
                    (site_id, point_key, ts, value, value_text, status)
                VALUES (?, ?, CAST(? AS TIMESTAMPTZ), ?, ?, ?)
                ON CONFLICT (site_id, point_key, ts) DO UPDATE SET
                    value = excluded.value,
                    value_text = excluded.value_text,
                    status = excluded.status
                """,
                [
                    [
                        rec["site_id"],
                        rec["point_key"],
                        rec["ts"],
                        rec["value"],
                        rec["value_text"],
                        rec["status"],
                    ]
                    for rec in batch
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                LOG.exception("rollback after failed flush")
            self._pending = batch + self._pending
            raise
        self._dirty_since_snapshot = True
        return len(batch)

    def sync_from_hot(self, hot: HotCache) -> int:
        """Ingest newer latest samples from HotCache into in-memory DuckDB (SI)."""
        snap = hot.snapshot_latest()
        if not snap:
            return 0
        with self._lock:
            self._flush_locked()
            cur = self.conn.execute(
                "SELECT point_key, epoch_us(MAX(ts)) FROM fdd.sample_store GROUP BY point_key"
            )
            latest_by_ref = {str(r[0]): int(r[1]) for r in cur.fetchall() if r[0] and r[1] is not None}
            pending: list[PointSample] = []
            # Use a lightweight synthetic sample for conversion / insert
            inserted = 0
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
                units = str(rec.get("units") or "")
                si_val, si_unit = to_si(v_num, units)
                site_id = str(rec.get("site") or "")
                attrs = {
                    "source": str(rec.get("source") or "hot-cache"),
                    "device": str(rec.get("device") or ""),
                    "object": str(rec.get("object") or ""),
                    "property": str(rec.get("property") or "present-value"),
                }
                self.conn.execute(
                    """
                    INSERT INTO fdd.point (
                        site_id, point_key, equipment_id, role, kind, unit, source_unit, attrs
                    ) VALUES (?, ?, ?, '', 'numeric', ?, ?, CAST(? AS JSON))
                    ON CONFLICT (site_id, point_key) DO NOTHING
                    """,
                    [
                        site_id,
                        ref,
                        _target_equipment(None, ref),
                        si_unit or normalize_si_unit(units) or None,
                        units or None,
                        _json_dumps(attrs),
                    ],
                )
                self.conn.execute(
                    """
                    INSERT INTO fdd.sample_store
                        (site_id, point_key, ts, value, value_text, status)
                    VALUES (?, ?, CAST(? AS TIMESTAMPTZ), ?, ?, ?)
                    ON CONFLICT (site_id, point_key, ts) DO NOTHING
                    """,
                    [
                        site_id,
                        ref,
                        ts,
                        si_val,
                        None if si_val is not None else v_txt,
                        str(rec.get("status") or "ok"),
                    ],
                )
                inserted += 1
            if inserted:
                self._dirty_since_snapshot = True
                self._prune_memory_locked()
            del pending
            return inserted

    def record_rule_results(
        self,
        rows: Sequence[dict[str, Any]],
        *,
        poll_seconds: float = 300.0,
        engine: str = "duckdb",
    ) -> int:
        """Upsert Open-FDD fdd.rule_result rows (latest assessment per subject/rule)."""
        if not rows:
            return 0
        with self._lock:
            n = self._upsert_rule_results_locked(rows, poll_seconds=poll_seconds, engine=engine)
            self._dirty_since_snapshot = True
            self._prune_memory_locked()
            return n

    def _upsert_rule_results_locked(
        self,
        rows: Sequence[dict[str, Any]],
        *,
        poll_seconds: float = 300.0,
        engine: str = "duckdb",
    ) -> int:
        n = 0
        for row in rows:
            site_id = str(row.get("site_id") or "")
            equipment_id = str(row.get("equipment_id") or "edge")
            rule_id = str(row.get("rule_id") or "")
            subject_key = str(row.get("subject_key") or row.get("series_id") or "")
            if not rule_id:
                continue
            status = str(row.get("status") or "PASS").upper()
            if status not in RULE_RESULT_STATUSES:
                status = "FAULT" if float(row.get("fault_hours") or 0) > 0 else "PASS"
            window_start = row.get("window_start")
            window_end = row.get("window_end")
            if not window_end or not window_start:
                # Keep a single latest row per subject: delete prior, insert assessed window.
                pass
            self.conn.execute(
                """
                DELETE FROM fdd.rule_result
                WHERE site_id = ? AND equipment_id = ? AND rule_id = ? AND subject_key = ?
                  AND rule_hash = '' AND config_hash = ''
                """,
                [site_id, equipment_id, rule_id, subject_key],
            )
            fault_hours = row.get("fault_hours")
            covered_hours = row.get("covered_hours")
            fault_pct = row.get("fault_pct")
            if fault_pct is None and fault_hours is not None and covered_hours:
                try:
                    if float(covered_hours) > 0:
                        fault_pct = 100.0 * float(fault_hours) / float(covered_hours)
                except (TypeError, ValueError):
                    fault_pct = None
            # window_end > window_start is required; default to a one-hour lookback.
            if not window_end:
                window_end = utc_now_iso()
            if not window_start:
                try:
                    end_dt = datetime.fromisoformat(str(window_end).replace("Z", "+00:00"))
                    hours = float(covered_hours) if covered_hours else 1.0
                    hours = max(hours, 1.0 / 60.0)
                    window_start = (datetime.fromtimestamp(end_dt.timestamp() - hours * 3600, tz=timezone.utc)).strftime(
                        "%Y-%m-%dT%H:%M:%S.%f"
                    )[:-3] + "Z"
                except (TypeError, ValueError, OSError):
                    window_start = "1970-01-01T00:00:00Z"
            params = [
                site_id,
                equipment_id,
                rule_id,
                subject_key,
                window_start,
                window_end,
                _json_dumps(row.get("effective_parameters") or {}),
                float(row.get("poll_seconds") or poll_seconds),
                status,
                row.get("reason"),
                fault_hours,
                covered_hours,
                fault_pct,
                row.get("role") or row.get("sensor_role"),
                row.get("engine") or engine,
                _json_dumps(row.get("metrics") or {}),
                _json_dumps(row.get("diagnostics") or {}),
            ]
            self.conn.execute(
                """
                INSERT INTO fdd.rule_result (
                    site_id, equipment_id, rule_id, subject_key,
                    window_start, window_end, effective_parameters, poll_seconds,
                    status, reason, fault_hours, covered_hours, fault_pct,
                    role, engine, metrics, diagnostics
                ) VALUES (
                    ?, ?, ?, ?, CAST(? AS TIMESTAMPTZ), CAST(? AS TIMESTAMPTZ),
                    ?::JSON, ?, ?, ?, ?, ?, ?, ?, ?, ?::JSON, ?::JSON
                )
                """,
                params,
            )
            n += 1
        return n

    def _enforce_disk_cap_locked(self) -> None:
        """Drop oldest complete generations. Never delete the newest one.

        Legacy telemetry and fdd_results files are not part of a generation.
        They are removed once a modern generation exists and are not counted
        toward the byte cap, so they cannot evict the snapshot just written.
        """
        if self._generations():
            self._delete_legacy_files()
        gens = self._generations()
        while len(gens) > self.max_snapshots and len(gens) > 1:
            self._remove_generation(gens.pop(0))
        while len(gens) > 1 and sum(gen.nbytes() for gen in gens) > self.max_bytes:
            self._remove_generation(gens.pop(0))

    def _copy_parquet(self, select_sql: str, dest: Path, params: Sequence[Any] = ()) -> None:
        self.conn.execute(
            f"COPY ({select_sql}) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
            [*params, str(dest)],
        )
        _fsync_file(dest)

    def _copy_export_locked(self, staging: Path, hours: float) -> None:
        window = "CURRENT_TIMESTAMP - (CAST(? AS DOUBLE) * INTERVAL '1' HOUR)"
        self._copy_parquet(
            f"""
            SELECT site_id, point_key, ts, value
            FROM fdd.input_sample
            WHERE ts >= {window}
            ORDER BY ts ASC
            """,
            staging / "input_sample.parquet",
            [hours],
        )
        self._copy_parquet(
            f"SELECT {', '.join(POINT_COLUMNS)} FROM fdd.point",
            staging / "point.parquet",
        )
        self._copy_parquet(
            f"""
            SELECT {", ".join(RULE_RESULT_COLUMNS)}
            FROM fdd.rule_result
            WHERE window_end >= {window}
            ORDER BY window_end ASC
            """,
            staging / "rule_result.parquet",
            [hours],
        )

    def write_snapshot(self, *, force: bool = False) -> Path | None:
        """Write one fsynced generation (sample, point, optional rule, manifest last)."""
        with self._lock:
            self._flush_locked()
            self._prune_memory_locked()
            cnt = self.row_count_locked()
            if cnt == 0:
                return None
            existing = self._generations() or list(self.snapshot_dir.glob("telemetry_*.parquet"))
            if not force and not self._dirty_since_snapshot and existing:
                return None
            now = datetime.now(timezone.utc)
            if not self._clock_allows_snapshot(now):
                return None

            seq = 1
            gens = self._generations()
            if gens and isinstance(gens[-1].key[1], int) and gens[-1].key[0] == 1:
                seq = int(gens[-1].key[1]) + 1
            elif gens:
                seq = max((int(item.key[1]) for item in gens if item.key[0] == 1), default=0) + 1
            staging = self.snapshot_dir / f".gen_{seq:06d}.tmp"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir()
            final_s = staging / "sample_store.parquet"
            self._copy_parquet(
                """
                SELECT site_id, point_key, ts, value, value_text, status
                FROM fdd.sample_store
                ORDER BY ts ASC
                """,
                final_s,
            )
            final_p = staging / "point.parquet"
            self._copy_parquet(
                f"SELECT {', '.join(POINT_COLUMNS)} FROM fdd.point",
                final_p,
            )
            files = ["sample_store.parquet", "point.parquet"]
            rcnt = int(self.conn.execute("SELECT COUNT(*) FROM fdd.rule_result").fetchone()[0])
            if rcnt > 0:
                final_r = staging / "rule_result.parquet"
                self._copy_parquet(
                    f"""
                    SELECT {", ".join(RULE_RESULT_COLUMNS)} FROM fdd.rule_result
                    ORDER BY window_end ASC
                    """,
                    final_r,
                )
                files.append("rule_result.parquet")
            manifest = {
                "seq": seq,
                "files": files,
                "written_utc": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            }
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
            _fsync_file(manifest_path)
            _fsync_dir(staging)
            final_dir = self.snapshot_dir / f"gen_{seq:06d}"
            os.replace(staging, final_dir)
            _fsync_dir(self.snapshot_dir)
            self._enforce_disk_cap_locked()
            self._dirty_since_snapshot = False
            self.last_snapshot_utc = utc_now_iso()
            self._clock_sane = True
            return final_dir / "sample_store.parquet"

    def export_bundle_file(
        self,
        targets: Sequence[PointTarget],
        *,
        hours: float = 24.0,
        graph: Any = None,
    ) -> Path:
        """Copy the export under the store lock, then zip it outside the lock.

        The zip is written under the snapshot directory, not ``/tmp``. The
        caller streams it and deletes it.
        """
        hours = max(0.01, min(float(hours), 720.0))
        if targets:
            self.upsert_points(targets, graph=graph)
        export_root = self.snapshot_dir / "exports"
        export_root.mkdir(parents=True, exist_ok=True)
        token = f"{threading.get_ident()}-{os.urandom(4).hex()}"
        staging = export_root / f".staging-{token}"
        staging.mkdir()
        zip_path = export_root / f"bundle-{token}.zip"
        try:
            with self._lock:
                self._flush_locked()
                self._copy_export_locked(staging, hours)
            with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                for name in BUNDLE_FILES:
                    zf.write(staging / name, arcname=name)
        except Exception:
            zip_path.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return zip_path

    def export_bundle_zip(
        self,
        targets: Sequence[PointTarget],
        *,
        hours: float = 24.0,
        graph: Any = None,
    ) -> bytes:
        """Export Open-FDD core Parquet tables (SI) as a ZIP bundle."""
        path = self.export_bundle_file(targets, hours=hours, graph=graph)
        try:
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)

    def query(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        with self._lock:
            return list(self.conn.execute(sql, params or []).fetchall())

    def query_readonly(self, sql: str, params: list[Any] | None = None, *, limit: int = 500) -> dict[str, Any]:
        """Run one read-only statement against the in-memory store."""
        from edge_microdot.sql_guard import validate_read_only_sql

        cleaned = validate_read_only_sql(sql)
        cap = max(1, min(int(limit), 1000))
        bound = list(params or [])
        with self._lock:
            cur = self.conn.execute(cleaned, bound)
            if cur.description is None:
                return {"columns": [], "rows": [], "truncated": False}
            columns = [d[0] for d in cur.description]
            fetched = cur.fetchmany(cap + 1)
        truncated = len(fetched) > cap
        rows = [[_json_cell(cell) for cell in row] for row in fetched[:cap]]
        return {"columns": columns, "rows": rows, "truncated": truncated}

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
            try:
                self._flush_locked()
            except Exception:
                LOG.exception("flush on close failed")
            self.conn.close()
