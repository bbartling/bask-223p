"""SD-card mode snapshots Parquet and drains the log file on an interval."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from edge_microdot.cli import resolve_snapshot_interval
from edge_microdot.history_store import HistoryStore
from edge_microdot.logging_setup import configure_logging, drain_logs
from edge_microdot.models import PointSample


def _sample(i: int) -> PointSample:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return PointSample(
        timestamp_utc=ts,
        site="demo",
        source="fake",
        device="1",
        object=f"analog-input,{i}",
        property="present-value",
        value=float(i),
        units="degC",
    )


def test_memory_flush_does_not_write_parquet(tmp_path):
    store = HistoryStore(tmp_path / "h.duckdb", batch_size=1000)
    store.stage([_sample(i) for i in range(20)])
    assert store.conn.execute("SELECT COUNT(*) FROM fdd.sample_store").fetchone()[0] == 0
    assert store.snapshot_count() == 0
    assert store.flush() == 20
    assert store.conn.execute("SELECT COUNT(*) FROM fdd.sample_store").fetchone()[0] == 20
    assert list(store.snapshot_dir.glob("*.parquet")) == []
    assert store.write_snapshot(force=True) is not None
    assert list(store.snapshot_dir.glob("sample_store_*.parquet"))
    store.close()


def test_sd_card_snapshot_interval_uses_durable_minutes(monkeypatch):
    monkeypatch.delenv("EDGE_SNAPSHOT_INTERVAL", raising=False)
    monkeypatch.setenv("EDGE_DURABLE_MINUTES", "15")
    assert resolve_snapshot_interval(None, sd_card=True) == 900.0
    monkeypatch.setenv("EDGE_SNAPSHOT_INTERVAL", "120")
    assert resolve_snapshot_interval(None, sd_card=True) == 120.0
    assert resolve_snapshot_interval(45, sd_card=True) == 45.0


def test_log_file_waits_for_drain(tmp_path, monkeypatch):
    import edge_microdot.logging_setup as logs

    log_path = tmp_path / "edge.log"
    monkeypatch.setenv("EDGE_LOG_FILE", str(log_path))
    monkeypatch.setenv("EDGE_LOG_FLUSH_MINUTES", "15")
    monkeypatch.setenv("EDGE_LOG_LEVEL", "INFO")
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers.clear()
    logs._buffer = None
    try:
        configure_logging()
        logging.getLogger("edge.test").info("held in ram")
        assert not log_path.exists() or log_path.stat().st_size == 0
        assert drain_logs(force=False) == 0
        assert drain_logs(force=True) == 1
        assert "held in ram" in log_path.read_text(encoding="utf-8")
    finally:
        root.handlers.clear()
        root.handlers.extend(saved)
        logs._buffer = None
