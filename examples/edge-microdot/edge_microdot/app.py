"""Microdot HTTP API for edge ingest."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Sequence

from microdot import Microdot, Response, send_file
from microdot.websocket import websocket_upgrade

from edge_microdot.cache import HotCache
from edge_microdot.config import LoadedConfig
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointSample, PointTarget
from edge_microdot.sparql_kb import SparqlKb

LOG = logging.getLogger(__name__)

DEFAULT_SPARQL = """PREFIX s223: <http://data.ashrae.org/standard223#>
PREFIX ref: <https://brickschema.org/schema/Brick/ref#>
SELECT ?point ?label ?ref WHERE {
  { ?point s223:hasExternalReference ?r } UNION { ?point ref:hasExternalReference ?r }
  ?r ref:BACnetURI ?ref .
  OPTIONAL { ?point <http://www.w3.org/2000/01/rdf-schema#label> ?label }
}
LIMIT 50"""


class LiveHub:
    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: Any) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: Any) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def count(self) -> int:
        async with self._lock:
            return len(self._clients)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        message = json.dumps(payload)
        async with self._lock:
            clients = list(self._clients)
        dead: list[Any] = []
        for ws in clients:
            try:
                await ws.send(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


def create_app(
    *,
    source: Any,
    source_kind: str,
    targets: Sequence[PointTarget],
    cfg: LoadedConfig,
    hot: HotCache,
    history: HistoryStore,
    fdd: FddEngine,
    sparql: SparqlKb,
    site: str,
    poll_interval_s: float,
    flush_interval_s: float,
    fdd_interval_s: float,
) -> Microdot:
    app = Microdot()
    hub = LiveHub()
    targets_list = targets if isinstance(targets, list) else list(targets)
    poll_lock = asyncio.Lock()
    static_dir = Path(__file__).resolve().parent / "static"

    async def ingest_samples(samples: Sequence[PointSample]) -> None:
        if not samples:
            return
        hot.write(samples)
        history.stage(samples)
        await hub.broadcast(
            {"type": "samples", "count": len(samples), "samples": [s.to_dict() for s in samples]}
        )

    async def poll_once() -> list[PointSample]:
        async with poll_lock:
            if hasattr(source, "poll_once_async"):
                samples = await source.poll_once_async()
            else:
                samples = await asyncio.to_thread(source.poll_once)
            await ingest_samples(samples)
            return samples

    async def poll_loop() -> None:
        while True:
            try:
                await poll_once()
            except Exception:
                LOG.exception("poll failed")
            await asyncio.sleep(max(0.5, poll_interval_s))

    async def flush_loop() -> None:
        while True:
            await asyncio.sleep(max(1.0, flush_interval_s))
            try:
                n = history.flush()
                if n:
                    LOG.debug("flushed %s rows", n)
            except Exception:
                LOG.exception("flush failed")

    async def fdd_loop() -> None:
        while True:
            await asyncio.sleep(max(1.0, fdd_interval_s))
            try:
                latest = hot.snapshot_latest()
                fdd.run(latest, targets_list)
            except Exception:
                LOG.exception("fdd failed")

    def start_background_workers() -> None:
        """Start poll/flush/FDD asyncio loops on a daemon thread (Microdot has no ASGI lifespan)."""

        async def _supervisor() -> None:
            await asyncio.gather(poll_loop(), flush_loop(), fdd_loop())

        import threading

        threading.Thread(target=lambda: asyncio.run(_supervisor()), daemon=True).start()

    app.start_background_workers = start_background_workers  # type: ignore[attr-defined]

    @app.route("/")
    async def index(request):
        index_path = static_dir / "index.html"
        return send_file(str(index_path))

    @app.route("/static/<path:path>")
    async def static_files(request, path):
        return send_file(str(static_dir / path))

    @app.get("/health")
    async def health(request):
        return {
            "status": "ok",
            "site": site,
            "source": source_kind,
            "point_count": len(targets_list),
            "latest_count": hot.count,
            "last_updated_utc": hot.last_updated_utc,
            "ws_clients": await hub.count(),
            "history_bytes": history.path.stat().st_size if history.path.exists() else 0,
            "sparql_enabled": sparql.enabled,
            "sparql_triples": sparql.triple_count,
            "hot_backend": hot.backend,
        }

    @app.get("/points")
    async def list_points(request):
        device = request.args.get("device")
        object_type = request.args.get("object_type")
        out = []
        for t in targets_list:
            if device and t.device != device:
                continue
            if object_type and not t.object_id.startswith(f"{object_type},"):
                continue
            out.append(t.to_dict())
        return {"count": len(out), "points": out}

    @app.get("/latest")
    async def latest(request):
        rows = hot.snapshot_latest()
        return {"count": len(rows), "samples": rows}

    @app.get("/history")
    async def get_history(request):
        ref = request.args.get("ref")
        start = request.args.get("start")
        end = request.args.get("end")
        limit = int(request.args.get("limit", "200"))
        rows = history.query_history(ref=ref, start=start, end=end, limit=min(limit, 5000))
        return {"store": "duckdb", "ref": ref, "count": len(rows), "samples": rows}

    @app.get("/rules")
    async def rules(request):
        return fdd.snapshot()

    @app.post("/poll/once")
    async def trigger_poll(request):
        samples = await poll_once()
        return {"count": len(samples), "samples": [s.to_dict() for s in samples]}

    @app.post("/sparql")
    async def run_sparql(request):
        if not sparql.enabled:
            return Response.body("SPARQL disabled", status_code=503)
        body = request.json or {}
        query = body.get("query") or DEFAULT_SPARQL
        try:
            return sparql.query(query)
        except Exception as exc:
            return Response.body(str(exc), status_code=400)

    @app.get("/sparql/example")
    async def sparql_example(request):
        if not sparql.enabled:
            return {"enabled": False, "example_query": DEFAULT_SPARQL}
        try:
            result = sparql.query(DEFAULT_SPARQL)
            return {"enabled": True, "example_query": DEFAULT_SPARQL, "preview": result}
        except Exception as exc:
            return {"enabled": True, "error": str(exc)}

    @app.route("/live")
    async def live_ws(request):
        ws = await websocket_upgrade(request)
        await hub.add(ws)
        try:
            snap = hot.snapshot_latest()
            if snap:
                await ws.send(json.dumps({"type": "snapshot", "count": len(snap), "samples": snap}))
            while True:
                await ws.receive()
        finally:
            await hub.remove(ws)

    return app