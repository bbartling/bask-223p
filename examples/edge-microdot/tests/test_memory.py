"""RSS measurement for idle-ish footprint with ~50 fake points."""
from __future__ import annotations

import resource

from edge_microdot.cache import HotCache
from edge_microdot.config import LoadedConfig
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointTarget
from edge_microdot.sources.fake import FakePollSource


def _rss_mb() -> float:
    # Linux ru_maxrss is kilobytes
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def test_rss_under_50_points(tmp_path):
    targets = [
        PointTarget(site="demo", device="10001", object_id=f"analog-input,{i}", units="C")
        for i in range(1, 51)
    ]
    hot = HotCache("dict")
    history = HistoryStore(tmp_path / "mem.duckdb")
    fdd = FddEngine(history=history)
    source = FakePollSource(targets)
    baseline = _rss_mb()
    samples = source.poll_once()
    hot.write(samples)
    history.stage(samples)
    history.flush()
    fdd.run(hot.snapshot_latest(), targets)
    after = _rss_mb()
    delta = after - baseline
    print(f"\nMEMORY_RSS_MB idle-ish={after:.1f} delta_after_50pt_poll={delta:.1f}")
    assert len(samples) == 50
    assert after < 256, f"RSS {after:.1f} MB unexpectedly high on dev host"
    history.close()
    hot.close()