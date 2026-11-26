from __future__ import annotations

from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore


def test_stale_and_range_rules(tmp_path):
    history = HistoryStore(tmp_path / "fdd.duckdb")
    engine = FddEngine(history=history, stale_minutes=5)
    history.conn.execute(
        """
        INSERT INTO fdd.point (site_id, point_key, equipment_id, role, unit, attrs)
        VALUES (
            'demo',
            'bacnet://1/analog-input,1',
            'AHU-1',
            'zone_air_temperature',
            'degC',
            '{"source":"fake","device":"1","object":"analog-input,1","property":"present-value"}'::JSON
        )
        """
    )
    history.conn.execute(
        """
        INSERT INTO fdd.sample_store (site_id, point_key, ts, value, value_text, status)
        VALUES (
            'demo',
            'bacnet://1/analog-input,1',
            CAST('2000-01-01T00:00:00Z' AS TIMESTAMPTZ),
            55.0, '55', 'ok'
        )
        """
    )
    faults = engine.run()
    ids = {f.rule_id for f in faults}
    assert "SV-STALE" in ids
    # Official result schema persisted
    rows = history.conn.execute(
        "SELECT rule_id, status, subject_key, role FROM fdd.rule_result WHERE rule_id = 'SV-STALE'"
    ).fetchall()
    assert rows
    assert rows[0][1] == "FAULT"
    assert rows[0][2] == "bacnet://1/analog-input,1"
    assert rows[0][3] == "zone_air_temperature"
    history.close()
