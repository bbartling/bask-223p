from __future__ import annotations

import pytest

from edge_microdot.app import create_app
from edge_microdot.cache import HotCache
from edge_microdot.config import LoadedConfig
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointTarget
from edge_microdot.sources.fake import FakePollSource
from edge_microdot.sparql_kb import SparqlKb


@pytest.fixture
def edge_app(tmp_path):
    targets = [
        PointTarget(site="demo", device="10001", object_id=f"analog-input,{i}", units="C")
        for i in range(1, 6)
    ]
    source = FakePollSource(targets)
    hot = HotCache("dict")
    history = HistoryStore(tmp_path / "h.duckdb", max_bytes=5 * 1024 * 1024)
    fdd = FddEngine(history=history, stale_minutes=999)
    app = create_app(
        source=source,
        source_kind="fake",
        targets=targets,
        cfg=LoadedConfig(site="demo"),
        hot=hot,
        history=history,
        fdd=fdd,
        sparql=SparqlKb(None),
        site="demo",
        poll_interval_s=3600,
        flush_interval_s=3600,
        fdd_interval_s=3600,
    )
    yield app
    history.close()
    hot.close()


def test_health_and_latest(edge_app):
    import asyncio

    from microdot.test_client import TestClient

    async def _run() -> None:
        client = TestClient(edge_app)
        h = await client.get("/health")
        assert h.status_code == 200
        assert h.json["status"] == "ok"
        p = await client.post("/poll/once")
        assert p.status_code == 200
        latest = await client.get("/latest")
        body = latest.json
        assert body["count"] == 5

    asyncio.run(_run())