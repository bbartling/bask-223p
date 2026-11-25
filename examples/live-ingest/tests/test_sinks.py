from __future__ import annotations

import csv
import io

import pytest

from live_ingest.models import PointSample
from live_ingest.sinks import CSV_COLUMNS, CsvSink, DuckDbSink, StdoutSink


def test_csv_sink_writes_expected_header_once(tmp_path):
    csv_path = tmp_path / "telemetry.csv"
    sink = CsvSink(csv_path)
    s1 = PointSample("2026-11-25T12:00:00.000Z", "s1", "baskstream", "5007", "analog-input,1173", "present-value", 76.7, "DEG_F", "ok")
    s2 = PointSample("2026-11-25T12:01:00.000Z", "s1", "baskstream", "5007", "analog-input,1173", "present-value", 76.9, "DEG_F", "ok")
    sink.write([s1])
    sink.write([s2])
    sink.close()

    with csv_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert len(rows) == 3
    assert rows[1][6] == "76.7"


def test_stdout_sink_json_and_line():
    buf = io.StringIO()
    sink = StdoutSink(fmt="json", stream=buf)
    sink.write([PointSample("2026-11-25T12:00:00Z", "s1", "bacnet-rpm", "10001", "analog-input,1", "present-value", 55.2, "DEG_F", "ok")])
    assert "bacnet://10001/analog-input,1" in buf.getvalue()


def test_duckdb_sink_history_and_readonly_sql(tmp_path):
    db_path = tmp_path / "test.duckdb"
    sink = DuckDbSink(db_path)
    sink.write([
        PointSample("2026-11-25T12:00:00Z", "s1", "bacnet-rpm", "10001", "analog-input,1", "present-value", 55.0, "DEG_F", "ok"),
        PointSample("2026-11-25T12:05:00Z", "s1", "bacnet-rpm", "10001", "analog-input,1", "present-value", 56.0, "DEG_F", "ok"),
        PointSample("2026-11-25T12:05:00Z", "s1", "baskstream", "10001", "binary-output,1", "present-value", True, "", "ok"),
    ])

    hist = sink.query_history(
        ref="bacnet://10001/analog-input,1",
        start="2026-11-25T12:01:00Z",
        end="2026-11-25T12:10:00Z",
    )
    assert len(hist) == 1
    assert hist[0]["value"] == pytest.approx(56.0)

    sql_res = sink.query_sql("SELECT ref, AVG(value_num) AS avg_v FROM telemetry GROUP BY ref ORDER BY ref")
    assert sql_res["count"] == 2
    assert sql_res["rows"][0]["avg_v"] == pytest.approx(55.5)

    for bad in ("DROP TABLE telemetry", "DELETE FROM telemetry", "SELECT 1; DROP TABLE telemetry"):
        with pytest.raises(ValueError):
            sink.query_sql(bad)
    sink.close()
