"""Sample ingest must not clobber point metadata, and a failed flush keeps the batch."""
from __future__ import annotations

import json

import pytest

from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointSample, PointTarget


def _sample(value: float = 21.0, *, minute: int = 0) -> PointSample:
    return PointSample(
        timestamp_utc=f"2026-06-01T00:{minute:02d}:00Z",
        site="demo",
        source="fake",
        device="10",
        object="analog-input,1",
        property="present-value",
        value=value,
        units="degC",
    )


def test_flush_does_not_overwrite_point_metadata(tmp_path):
    store = HistoryStore(tmp_path / "snaps")
    target = PointTarget(
        site="demo",
        device="10",
        object_id="analog-input,1",
        units="degC",
        label="Zone Temp",
        extra={"equipment": "AHU-1", "haystack_tags": "zone air temp sensor", "class_223p": "s223:TemperatureSensor"},
    )
    store.upsert_points([target])
    store.stage([_sample()])
    assert store.flush() == 1
    equipment, attrs, role = store.query("SELECT equipment_id, attrs::VARCHAR, role FROM fdd.point")[0]
    parsed = json.loads(attrs)
    assert equipment == "AHU-1"
    assert role == "zone_air_temperature"
    assert parsed["class_223p"] == "s223:TemperatureSensor"
    assert parsed["label"] == "Zone Temp"
    store.close()


def test_failed_flush_restores_pending_batch(tmp_path):
    store = HistoryStore(tmp_path / "snaps", batch_size=100)
    store.stage([_sample(minute=0), _sample(22.0, minute=1)])
    store._pending[1]["ts"] = "not-a-timestamp"
    with pytest.raises(Exception):
        store.flush()
    assert store.row_count() == 0
    assert len(store._pending) == 2
    store.close()


def test_stage_respects_batch_size(tmp_path):
    store = HistoryStore(tmp_path / "snaps", batch_size=3)
    store.stage([_sample(minute=0), _sample(22.0, minute=1)])
    assert store.row_count() == 0
    store.stage([_sample(23.0, minute=2)])
    assert store.row_count() == 3
    store.close()
