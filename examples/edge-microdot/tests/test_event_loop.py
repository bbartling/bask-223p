"""Workers share Microdot's loop, and a slow live client is dropped."""
from __future__ import annotations

import asyncio

from edge_microdot.app import LiveHub, create_app
from edge_microdot.cache import HotCache
from edge_microdot.config import LoadedConfig
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointTarget
from edge_microdot.security import load_auth_from_env
from edge_microdot.sources.fake import FakePollSource
from edge_microdot.sparql_kb import SparqlKb


class _SlowSocket:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)
        self.started.set()
        await self.release.wait()


def test_workers_are_tasks_on_the_running_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGE_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setenv("EDGE_AUTH_DISABLED", "1")
    targets = [PointTarget(site="demo", device="1", object_id="analog-input,1", units="C")]
    history = HistoryStore(tmp_path / "h.duckdb")
    app = create_app(
        source=FakePollSource(targets),
        source_kind="fake",
        targets=targets,
        cfg=LoadedConfig(site="demo"),
        hot=HotCache("dict"),
        history=history,
        fdd=FddEngine(history=history),
        sparql=SparqlKb(None),
        auth=load_auth_from_env(),
        site="demo",
        poll_interval_s=3600,
        flush_interval_s=3600,
        fdd_interval_s=3600,
        snapshot_interval_s=3600,
    )

    async def _run() -> None:
        tasks = app.start_background_workers()
        try:
            loop = asyncio.get_running_loop()
            assert len(tasks) == 4
            assert all(task.get_loop() is loop for task in tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_run())
    history.close()


def test_slow_live_client_is_dropped_and_the_cap_holds():
    async def _run() -> None:
        hub = LiveHub(max_clients=1, queue_size=1)
        slow = _SlowSocket()
        assert await hub.add(slow)
        extra = _SlowSocket()
        assert await hub.add(extra) is False
        await hub.broadcast({"n": 1})
        await slow.started.wait()
        await hub.broadcast({"n": 2})
        await hub.broadcast({"n": 3})
        assert await hub.count() == 0
        slow.release.set()

    asyncio.run(_run())
