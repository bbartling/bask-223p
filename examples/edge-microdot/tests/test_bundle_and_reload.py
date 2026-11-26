from __future__ import annotations

import asyncio
import base64
import io
import zipfile
from datetime import datetime, timezone

import duckdb
import pytest

from edge_microdot.cache import HotCache
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointSample, PointTarget
from edge_microdot.units_si import to_si


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def test_si_unit_conversion():
    c, u = to_si(68.0, "°F")
    assert u == "degC"
    assert c == pytest.approx(20.0)

    pa, pu = to_si(1.0, "in_h2o")
    assert pu == "Pa"
    assert pa == pytest.approx(249.08891)

    flow, fu = to_si(1000.0, "cfm")
    assert fu == "m3/s"
    assert flow == pytest.approx(0.47194745)

    w, wu = to_si(2.5, "kW")
    assert wu == "W"
    assert w == pytest.approx(2500.0)


def test_in_memory_snapshot_reload_and_rotation(tmp_path):
    snap_dir = tmp_path / "var" / "edge_history.duckdb"
    store1 = HistoryStore(snap_dir, max_bytes=1024 * 1024, max_snapshots=2)
    assert not snap_dir.exists(), "no growing .duckdb file should be created on disk"

    hot = HotCache("dict")
    ts = _now_iso()
    hot.write(
        [
            PointSample(
                timestamp_utc=ts,
                site="demo",
                source="fake",
                device="10001",
                object="analog-input,1",
                property="present-value",
                value=72.5,
                units="°F",
            ),
            PointSample(
                timestamp_utc=ts,
                site="demo",
                source="fake",
                device="10001",
                object="analog-input,2",
                property="present-value",
                value=21.5,
                units="degC",
            ),
        ]
    )
    inserted = store1.sync_from_hot(hot)
    assert inserted == 2
    assert store1.sync_from_hot(hot) == 0  # deduplicated

    for _ in range(3):
        snap = store1.write_snapshot(force=True)
        assert snap is not None and snap.exists()
    assert store1.snapshot_count() <= 2
    assert store1.disk_bytes() <= 1024 * 1024
    store1.close()

    # Reload into a brand-new in-memory DuckDB instance
    store2 = HistoryStore(snap_dir, max_bytes=1024 * 1024, max_snapshots=2)
    assert store2.row_count() == 2
    rows = store2.query_history()
    assert {r["ref"] for r in rows} == {
        "bacnet://10001/analog-input,1",
        "bacnet://10001/analog-input,2",
    }
    store2.close()
    hot.close()


def test_export_bundle_parquet_schemas_and_sql_rules(tmp_path):
    store = HistoryStore(tmp_path / "snaps", max_bytes=5 * 1024 * 1024)
    targets = [
        PointTarget(
            site="demo",
            device="10001",
            object_id="analog-input,1",
            units="°F",
            label="Zone Temp",
            extra={"equipment": "AHU-1", "haystack_tags": "zone air temp sensor", "class_223p": "s223:TemperatureSensor"},
        ),
        PointTarget(
            site="demo",
            device="10001",
            object_id="analog-input,2",
            units="cfm",
            label="Supply Airflow",
            extra={"equipment": "AHU-1", "haystack_tags": "discharge air flow sensor", "class_223p": "s223:FlowSensor"},
        ),
    ]
    ts_old = "2000-01-01T00:00:00Z"
    ts_now = _now_iso()
    store.stage(
        [
            PointSample(
                timestamp_utc=ts_now,
                site="demo",
                source="fake",
                device="10001",
                object="analog-input,1",
                property="present-value",
                value=68.0,
                units="°F",
            ),
            PointSample(
                timestamp_utc=ts_now,
                site="demo",
                source="fake",
                device="10001",
                object="analog-input,2",
                property="present-value",
                value=1000.0,
                units="cfm",
            ),
            PointSample(
                timestamp_utc=ts_old,
                site="demo",
                source="fake",
                device="10001",
                object="analog-input,1",
                property="present-value",
                value=68.0,
                units="°F",
            ),
        ]
    )
    store.flush()
    engine = FddEngine(history=store, stale_minutes=5, targets=targets)
    # Also ensure an FDD result row is recorded
    store.record_rule_results([{"equipment_id": "AHU-1", "rule_id": "SV-STALE", "subject_key": "bacnet://10001/analog-input,1", "status": "FAULT", "reason": "test fault", "fault_hours": 1.0, "covered_hours": 1.0, "role": "zone_air_temperature", "window_end": ts_now}])

    raw_zip = store.export_bundle_zip(targets, hours=24)
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        assert set(zf.namelist()) == {"input_sample.parquet", "point.parquet", "rule_result.parquet"}
        for name in zf.namelist():
            (tmp_path / name).write_bytes(zf.read(name))

    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone = 'UTC'")
    s_cols = [d[0] for d in con.execute(f"SELECT * FROM read_parquet('{tmp_path / 'input_sample.parquet'}') LIMIT 0").description]
    p_cols = [d[0] for d in con.execute(f"SELECT * FROM read_parquet('{tmp_path / 'point.parquet'}') LIMIT 0").description]
    r_cols = [d[0] for d in con.execute(f"SELECT * FROM read_parquet('{tmp_path / 'rule_result.parquet'}') LIMIT 0").description]

    from edge_microdot.history_store import INPUT_SAMPLE_COLUMNS, POINT_COLUMNS, RULE_RESULT_COLUMNS
    assert s_cols == list(INPUT_SAMPLE_COLUMNS)
    assert p_cols == list(POINT_COLUMNS)
    assert r_cols == list(RULE_RESULT_COLUMNS)

    samples = con.execute(f"SELECT point_key, value FROM read_parquet('{tmp_path / 'input_sample.parquet'}') WHERE ts > TIMESTAMPTZ '2020-01-01' ORDER BY point_key").fetchall()
    assert len(samples) == 2
    assert samples[0][1] == pytest.approx(20.0)
    assert samples[1][1] == pytest.approx(0.47194745)

    pts = con.execute(f"SELECT point_key, equipment_id, role, unit FROM read_parquet('{tmp_path / 'point.parquet'}') ORDER BY point_key").fetchall()
    assert pts[0][0] == "bacnet://10001/analog-input,1"
    assert pts[0][1] == "AHU-1"
    assert pts[0][2] == "zone_air_temperature"
    assert pts[0][3] == "degC"
    assert pts[1][2] == "discharge_air_volumetric_flow"
    assert pts[1][3] == "m3/s"

    # Verify sql/fdd/rules/*.sql run unchanged against the Open-FDD views
    con.execute("CREATE SCHEMA IF NOT EXISTS fdd")
    con.execute(f"CREATE VIEW fdd.input_sample AS SELECT * FROM read_parquet('{tmp_path / 'input_sample.parquet'}')")
    con.execute(f"CREATE VIEW fdd.point AS SELECT * FROM read_parquet('{tmp_path / 'point.parquet'}')")
    con.execute("CREATE VIEW sensor_history AS SELECT p.equipment_id, p.point_key AS series_id, p.role AS sensor_role, s.ts AS timestamp_utc, s.value, p.unit FROM fdd.input_sample s JOIN fdd.point p ON p.site_id = s.site_id AND p.point_key = s.point_key")
    defaults = engine.default_rule_params()
    for rule_path in sorted(engine.sql_dir.glob("*.sql")):
        con.execute(rule_path.read_text(encoding="utf-8"), defaults[rule_path.name]).fetchall()

    con.close()
    store.close()


def test_bundle_and_sql_routes_auth(tmp_path, monkeypatch):
    from microdot.test_client import TestClient
    from edge_microdot.app import create_app
    from edge_microdot.config import LoadedConfig
    from edge_microdot.security import load_auth_from_env
    from edge_microdot.sources.fake import FakePollSource
    from edge_microdot.sparql_kb import SparqlKb

    monkeypatch.delenv("EDGE_AUTH_DISABLED", raising=False)
    targets = [PointTarget(site="demo", device="10001", object_id="analog-input,1", units="°F")]
    auth = load_auth_from_env()
    history = HistoryStore(tmp_path / "snaps")
    app = create_app(
        source=FakePollSource(targets),
        source_kind="fake",
        targets=targets,
        cfg=LoadedConfig(site="demo"),
        hot=HotCache("dict"),
        history=history,
        fdd=FddEngine(history=history, targets=targets),
        sparql=SparqlKb(None),
        auth=auth,
        site="demo",
        poll_interval_s=3600,
        flush_interval_s=3600,
        fdd_interval_s=3600,
    )

    async def _run():
        client = TestClient(app)
        assert (await client.get("/api/export/bundle")).status_code == 401
        assert (await client.get("/api/sql-rules")).status_code == 401
        assert (await client.get("/sql")).status_code == 302

        tok_resp = await client.post(
            "/auth/token",
            headers={"Authorization": "Basic " + base64.b64encode(b"operator:operator-pass").decode("ascii")},
        )
        assert tok_resp.status_code == 200
        bearer = {"Authorization": f"Bearer {tok_resp.json['access_token']}"}

        await client.post("/poll/once", headers=bearer)
        bundle_resp = await client.get("/api/export/bundle?hours=1", headers=bearer)
        assert bundle_resp.status_code == 200
        assert bundle_resp.headers.get("Content-Type") == "application/zip"
        with zipfile.ZipFile(io.BytesIO(bundle_resp.body)) as zf:
            assert set(zf.namelist()) == {"input_sample.parquet", "point.parquet", "rule_result.parquet"}

        rules_resp = await client.get("/api/sql-rules", headers=bearer)
        assert rules_resp.status_code == 200
        assert "duckdb-wasm" in rules_resp.json["duckdb_wasm_url"]
        assert any(r["filename"] == "sv_stale.sql" for r in rules_resp.json["rules"])

        login = await client.post(
            "/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body="username=operator&password=operator-pass",
        )
        assert login.status_code == 302
        sql_page = await client.get("/sql")
        assert sql_page.status_code == 200
        assert b"DuckDB-WASM" in sql_page.body

    asyncio.run(_run())
    history.close()


def test_legacy_telemetry_snapshot_bulk_reload(tmp_path):
    """Pre-parity telemetry_*.parquet files must reload in bulk (not row-by-row)."""
    import time

    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone = 'UTC'")
    con.execute(
        """
        CREATE TABLE t AS
        SELECT
            'bacnet://10001/analog-input,' || CAST(i % 20 AS VARCHAR) AS ref,
            CURRENT_TIMESTAMP - ((4000 - i) * INTERVAL 15 SECOND) AS timestamp_utc,
            'demo' AS site,
            'fake' AS source,
            '10001' AS device,
            'analog-input,' || CAST(i % 20 AS VARCHAR) AS object,
            'present-value' AS property,
            68.0 + (i % 5) AS value_num,
            CAST(68.0 + (i % 5) AS VARCHAR) AS value_text,
            '°F' AS units,
            'ok' AS status
        FROM range(4000) t(i)
        """
    )
    out = snap_dir / "telemetry_20240101T000000Z.parquet"
    con.execute(f"COPY t TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    con.close()

    t0 = time.perf_counter()
    store = HistoryStore(snap_dir, max_bytes=5 * 1024 * 1024)
    elapsed = time.perf_counter() - t0
    assert store.row_count() == 4000
    rows = store.query_history(limit=5)
    assert rows
    # 68 F -> ~20 C
    assert rows[0]["units"] == "degC"
    assert rows[0]["value_num"] == pytest.approx(20.0, abs=0.1)
    assert elapsed < 8.0, f"legacy reload too slow ({elapsed:.2f}s); still row-by-row?"
    store.close()
