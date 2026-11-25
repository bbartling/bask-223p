from __future__ import annotations

from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointTarget


def test_stale_and_range_rules(tmp_path):
    history = HistoryStore(tmp_path / "fdd.duckdb")
    engine = FddEngine(history=history, stale_minutes=5)
    targets = [
        PointTarget(
            site="s",
            device="1",
            object_id="analog-input,1",
            ref="bacnet://1/analog-input,1",
            units="C",
            min_value=0,
            max_value=40,
        )
    ]
    latest = [
        {
            "ref": "bacnet://1/analog-input,1",
            "timestamp_utc": "2000-01-01T00:00:00.000Z",
            "status": "ok",
            "value_num": 55.0,
            "units": "C",
        }
    ]
    faults = engine.run(latest, targets)
    names = {f.rule_name for f in faults}
    assert "stale_data" in names
    assert "out_of_range" in names
    history.close()