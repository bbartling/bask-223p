from __future__ import annotations

from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
def test_stale_and_range_rules(tmp_path):
    history = HistoryStore(tmp_path / "fdd.duckdb")
    engine = FddEngine(history=history, stale_minutes=5)
    history.conn.execute(
        """
        INSERT INTO telemetry (ref, timestamp_utc, site, source, device, object, property,
            value_num, value_text, units, status)
        VALUES (
            'bacnet://1/analog-input,1',
            CAST('2000-01-01T00:00:00Z' AS TIMESTAMPTZ),
            's', 'fake', '1', 'analog-input,1', 'present-value',
            55.0, '55', 'C', 'ok'
        )
        """
    )
    faults = engine.run()
    names = {f.rule_name for f in faults}
    assert "stale_data" in names
    history.close()