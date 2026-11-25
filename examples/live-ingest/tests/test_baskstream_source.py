from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fake_baskstream_station import FakeBaskStreamStation  # noqa: E402

from bask_223p.bacnet.baskstream import READ_ONLY_OPS  # noqa: E402
from live_ingest.models import PointSample  # noqa: E402
from live_ingest.sources.baskstream_src import BaskStreamSource  # noqa: E402


def test_baskstream_poll_once_and_cov_against_fake_station():
    with FakeBaskStreamStation(cov_on_subscribe=True) as st:
        src = BaskStreamSource(
            station=st.url,
            username=st.user,
            password=st.password,
            base_ord="slot:/Drivers/BacnetNetwork",
            site="lab",
        )
        samples = src.poll_once()
        assert len(samples) == 9
        sa = next(s for s in samples if s.object == "analog-input,1")
        assert sa.value == pytest.approx(55.2)
        assert sa.ref == "bacnet://10001/analog-input,1"
        assert sa.status == "ok"

        cov_batches: list[list[PointSample]] = []
        count = src.stream_cov(cov_batches.append, duration_s=0.35, poll_timeout_s=0.1)
        assert count >= 18  # 9 initial subscribe + 9 COV frame
        assert any(s.source == "baskstream-cov" for batch in cov_batches for s in batch)
        assert st.state.write_attempts == []
        assert set(st.state.received_ops) <= READ_ONLY_OPS
