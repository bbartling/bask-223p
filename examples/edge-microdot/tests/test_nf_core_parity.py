"""Apples-to-apples checks against the Open-FDD NF DuckDB / core.sql contract."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from edge_microdot.fdd import FddEngine, RULE_FILE_IDS
from edge_microdot.history_store import (
    BUNDLE_FILES,
    HistoryStore,
    INPUT_SAMPLE_COLUMNS,
    POINT_COLUMNS,
    RULE_RESULT_COLUMNS,
    RULE_RESULT_STATUSES,
)
from edge_microdot.models import PointSample, PointTarget
from edge_microdot.role_terms import ACRONYM_TO_223P, RETIRED_ACRONYMS, input_term_from_target, to_input_term


def _cols(con, rel: str) -> list[str]:
    return [r[0] for r in con.execute(f"DESCRIBE {rel}").fetchall()]


def test_core_table_and_view_columns(tmp_path):
    store = HistoryStore(tmp_path / "snaps")
    con = store.conn
    assert _cols(con, "fdd.input_sample") == list(INPUT_SAMPLE_COLUMNS)
    point_cols = _cols(con, "fdd.point")
    assert point_cols == list(POINT_COLUMNS)
    result_cols = _cols(con, "fdd.rule_result")
    assert result_cols == list(RULE_RESULT_COLUMNS)
    config_cols = _cols(con, "fdd.rule_config")
    assert config_cols == [
        "site_id",
        "rule_id",
        "scope_key",
        "revision",
        "parameters",
        "active",
        "created_at",
        "created_by",
    ]
    sh = _cols(con, "sensor_history")
    assert sh == ["equipment_id", "series_id", "sensor_role", "timestamp_utc", "value", "unit"]
    store.close()


def test_223p_snake_case_input_terms_and_no_acronyms_in_sql():
    assert to_input_term("sat") == "discharge_air_temperature"
    assert to_input_term("zone air temp sensor") == "zone_air_temperature"
    assert to_input_term("discharge-air-temp") == "discharge_air_temperature"
    target = PointTarget(
        site="demo",
        device="10001",
        object_id="analog-input,1",
        units="°F",
        label="Zone Temp",
        extra={"equipment": "AHU-1", "haystack_tags": "zone air temp sensor"},
    )
    assert input_term_from_target(target) == "zone_air_temperature"

    rules_dir = Path(__file__).resolve().parent.parent / "sql" / "fdd" / "rules"
    sql_text = "\n".join(p.read_text(encoding="utf-8") for p in rules_dir.glob("*.sql"))
    # Retired acronyms must not appear as SQL identifiers / aliases.
    lowered = sql_text.lower()
    for acronym in RETIRED_ACRONYMS:
        for pattern in (f" as {acronym}", f" {acronym}.", f"={acronym}", f"role = '{acronym}'"):
            assert pattern not in lowered, f"retired acronym {acronym!r} leaked into SQL ({pattern!r})"
    for name in RULE_FILE_IDS:
        assert (rules_dir / name).is_file()


def test_bundle_and_rules_match_open_fdd_contract(tmp_path):
    store = HistoryStore(tmp_path / "snaps", max_bytes=5 * 1024 * 1024)
    targets = [
        PointTarget(
            site="demo",
            device="10001",
            object_id="analog-input,1",
            units="°F",
            label="Zone Temp",
            extra={
                "equipment": "AHU-1",
                "haystack_tags": "zone air temp sensor",
                "class_223p": "s223:TemperatureSensor",
            },
        ),
        PointTarget(
            site="demo",
            device="10001",
            object_id="analog-input,2",
            units="cfm",
            label="Supply Airflow",
            extra={
                "equipment": "AHU-1",
                "haystack_tags": "discharge air flow",
                "class_223p": "s223:FlowSensor",
            },
        ),
    ]
    ts_old = "2000-01-01T00:00:00Z"
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
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
    engine.run()

    raw_zip = store.export_bundle_zip(targets, hours=24)
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        assert set(zf.namelist()) == set(BUNDLE_FILES)
        for name in zf.namelist():
            (tmp_path / name).write_bytes(zf.read(name))

    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone = 'UTC'")
    s_cols = [d[0] for d in con.execute(f"SELECT * FROM read_parquet('{tmp_path / 'input_sample.parquet'}') LIMIT 0").description]
    p_cols = [d[0] for d in con.execute(f"SELECT * FROM read_parquet('{tmp_path / 'point.parquet'}') LIMIT 0").description]
    r_cols = [d[0] for d in con.execute(f"SELECT * FROM read_parquet('{tmp_path / 'rule_result.parquet'}') LIMIT 0").description]
    assert s_cols == list(INPUT_SAMPLE_COLUMNS)
    assert p_cols == list(POINT_COLUMNS)
    assert r_cols == list(RULE_RESULT_COLUMNS)

    samples = con.execute(
        f"SELECT point_key, value FROM read_parquet('{tmp_path / 'input_sample.parquet'}') "
        f"WHERE ts > TIMESTAMPTZ '2020-01-01' ORDER BY point_key"
    ).fetchall()
    assert len(samples) == 2
    assert samples[0][1] == pytest_approx(20.0)
    assert samples[1][1] == pytest_approx(0.47194745)

    roles = {
        row[0]: row[1:]
        for row in con.execute(
            f"SELECT point_key, role, unit, equipment_id FROM read_parquet('{tmp_path / 'point.parquet'}') ORDER BY point_key"
        ).fetchall()
    }
    assert roles["bacnet://10001/analog-input,1"][0] == "zone_air_temperature"
    assert roles["bacnet://10001/analog-input,1"][1] == "degC"
    assert roles["bacnet://10001/analog-input,1"][2] == "AHU-1"
    assert roles["bacnet://10001/analog-input,2"][0] == "discharge_air_volumetric_flow"
    assert roles["bacnet://10001/analog-input,2"][1] == "m3/s"
    assert "sat" not in {r[0] for r in roles.values()}

    statuses = set(
        con.execute(f"SELECT status FROM read_parquet('{tmp_path / 'rule_result.parquet'}')").fetchall()
    )
    assert statuses <= {(s,) for s in RULE_RESULT_STATUSES} or statuses <= RULE_RESULT_STATUSES or all(
        s[0] in RULE_RESULT_STATUSES for s in statuses
    )

    # Rules run unchanged against the bundle views (same tables /sql queries).
    con.execute("CREATE SCHEMA IF NOT EXISTS fdd")
    con.execute(f"CREATE VIEW fdd.input_sample AS SELECT * FROM read_parquet('{tmp_path / 'input_sample.parquet'}')")
    con.execute(f"CREATE VIEW fdd.point AS SELECT * FROM read_parquet('{tmp_path / 'point.parquet'}')")
    con.execute(f"CREATE VIEW fdd.rule_result AS SELECT * FROM read_parquet('{tmp_path / 'rule_result.parquet'}')")
    con.execute(
        "CREATE VIEW sensor_history AS SELECT p.equipment_id, p.point_key AS series_id, p.role AS sensor_role, "
        "s.ts AS timestamp_utc, s.value, p.unit FROM fdd.input_sample s "
        "JOIN fdd.point p ON p.site_id = s.site_id AND p.point_key = s.point_key"
    )
    defaults = engine.default_rule_params()
    for rule_path in sorted(engine.sql_dir.glob("*.sql")):
        rows = con.execute(rule_path.read_text(encoding="utf-8"), defaults[rule_path.name]).fetchall()
        cols = [d[0] for d in con.execute(rule_path.read_text(encoding="utf-8"), defaults[rule_path.name]).description]
        assert cols == ["equipment_id", "series_id", "sensor_role", "fault_hours", "covered_hours"]
        del rows

    con.close()
    store.close()


def pytest_approx(val, **kwargs):
    import pytest

    return pytest.approx(val, **kwargs)
