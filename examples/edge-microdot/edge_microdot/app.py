"""Microdot HTTP API for edge ingest."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote

from microdot import Microdot, Response, send_file
from microdot.session import with_session
from microdot.websocket import WebSocketError, websocket_upgrade

from edge_microdot.cache import HotCache
from edge_microdot.config import LoadedConfig
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointSample, PointTarget
from edge_microdot.security import AuthBundle, api_auth, attach_auth_routes, require_token
from edge_microdot.sparql_kb import SparqlKb
from edge_microdot.ui_api import attach_ui_api

LOG = logging.getLogger(__name__)

DEFAULT_DUCKDB_WASM_URL = "/static/vendor/duckdb/duckdb-browser.mjs"
DEFAULT_DUCKDB_WASM_BUNDLE_BASE = "/static/vendor/duckdb"

def resolve_static_file(static_dir: Path, raw_path: str) -> Path | None:
    """Return a regular file inside ``static_dir``, or None.

    Rejects absolute paths, ``..``, empty and dot segments, backslashes,
    double slashes, and null bytes, including percent-encoded forms. The
    resolved path must stay inside ``static_dir`` after following symlinks,
    so a symlink that points outside the tree is not served.
    """
    if not isinstance(raw_path, str) or raw_path == "":
        return None
    decoded = raw_path
    for _ in range(8):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    else:
        return None
    if _static_path_unsafe(raw_path) or _static_path_unsafe(decoded):
        return None
    root = static_dir.resolve()
    try:
        resolved = (root / decoded).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_relative_to(root) or resolved == root or not resolved.is_file():
        return None
    return resolved


def _static_path_unsafe(path: str) -> bool:
    if not path or path[0] in "/\\" or "\\" in path or "//" in path or "\x00" in path:
        return True
    return any(part in {"", ".", ".."} for part in path.split("/"))


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
    auth: AuthBundle,
    site: str,
    poll_interval_s: float,
    flush_interval_s: float,
    fdd_interval_s: float,
    snapshot_interval_s: float = 300.0,
) -> Microdot:
    app = Microdot()
    hub = LiveHub()
    targets_list = targets if isinstance(targets, list) else list(targets)
    poll_lock = asyncio.Lock()
    static_dir = Path(__file__).resolve().parent / "static"
    attach_auth_routes(app, auth)

    def _active_targets() -> list[PointTarget]:
        if targets_list:
            return targets_list
        src_targets = getattr(source, "targets", None)
        if src_targets:
            return list(src_targets)
        return []

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
                history.prune_memory()
                from edge_microdot.logging_setup import drain_logs

                drain_logs()
                if n:
                    LOG.debug("synced %s rows into in-memory DuckDB", n)
            except Exception:
                LOG.exception("flush failed")

    async def snapshot_loop() -> None:
        while True:
            wait_s = min(30.0, snapshot_interval_s) if history.snapshot_count() == 0 else max(5.0, snapshot_interval_s)
            await asyncio.sleep(wait_s)
            try:
                snap = await asyncio.to_thread(history.write_snapshot)
                if snap is not None:
                    LOG.info("wrote Parquet snapshot %s", snap.name)
            except Exception:
                LOG.exception("snapshot failed")

    async def fdd_loop() -> None:
        while True:
            await asyncio.sleep(max(1.0, fdd_interval_s))
            try:
                fdd.run(targets=_active_targets())
            except Exception:
                LOG.exception("fdd failed")

    def start_background_workers() -> None:
        """Start poll/flush/snapshot/FDD asyncio loops on a daemon thread."""

        async def _supervisor() -> None:
            await asyncio.gather(poll_loop(), flush_loop(), snapshot_loop(), fdd_loop())

        import threading

        threading.Thread(target=lambda: asyncio.run(_supervisor()), daemon=True).start()

    app.start_background_workers = start_background_workers  # type: ignore[attr-defined]

    @app.route("/")
    @auth.login
    async def index(request):
        index_path = static_dir / "index.html"
        return send_file(str(index_path))

    @app.route("/sql")
    @auth.login
    async def sql_page(request):
        sql_path = static_dir / "sql.html"
        return send_file(str(sql_path))

    @app.route("/static/<path:path>")
    async def static_files(request, path):
        target = resolve_static_file(static_dir, path)
        if target is None:
            return Response(status_code=404)
        return send_file(str(target))

    @app.get("/health")
    async def health(request):
        return {
            "status": "ok",
            "site": site,
            "source": source_kind,
            "point_count": len(_active_targets()),
            "latest_count": hot.count,
            "last_updated_utc": hot.last_updated_utc,
            "ws_clients": await hub.count(),
            "history_backend": "duckdb:memory",
            "history_rows": history.row_count(),
            "history_bytes": history.disk_bytes(),
            "snapshot_count": history.snapshot_count(),
            "last_snapshot_utc": history.last_snapshot_utc,
            "sparql_enabled": sparql.enabled,
            "sparql_triples": sparql.triple_count,
            "hot_backend": hot.backend,
            "auth_disabled": auth.disabled,
        }

    protect = api_auth(auth)

    @app.get("/points")
    @protect
    async def list_points(request):
        device = request.args.get("device")
        object_type = request.args.get("object_type")
        out = []
        for t in _active_targets():
            if device and t.device != device:
                continue
            if object_type and not t.object_id.startswith(f"{object_type},"):
                continue
            out.append(t.to_dict())
        return {"count": len(out), "points": out}

    @app.get("/latest")
    @protect
    async def latest(request):
        rows = hot.snapshot_latest()
        return {"count": len(rows), "samples": rows}

    @app.get("/history")
    @protect
    async def get_history(request):
        ref = request.args.get("ref")
        start = request.args.get("start")
        end = request.args.get("end")
        limit = int(request.args.get("limit", "200"))
        rows = history.query_history(ref=ref, start=start, end=end, limit=min(limit, 5000))
        return {"store": "duckdb:memory", "ref": ref, "count": len(rows), "samples": rows}

    @app.get("/rules")
    @protect
    async def rules(request):
        return fdd.snapshot()

    @app.get("/api/sql-rules")
    @protect
    async def sql_rules(request):
        wasm_url = (
            os.environ.get("EDGE_DUCKDB_WASM_URL", "").strip()
            or os.environ.get("EDGE_DUCKDB_WASM_CDN", "").strip()
            or DEFAULT_DUCKDB_WASM_URL
        )
        bundle_base = (
            os.environ.get("EDGE_DUCKDB_WASM_BUNDLE_BASE", "").strip()
            or DEFAULT_DUCKDB_WASM_BUNDLE_BASE
        )
        return {
            "duckdb_wasm_url": wasm_url,
            "duckdb_wasm_bundle_base": bundle_base,
            "rules": fdd.list_sql_rules(),
        }

    @app.get("/api/export/bundle")
    @protect
    async def export_bundle(request):
        raw_hours = request.args.get("hours", "24")
        try:
            hours = float(raw_hours)
        except (TypeError, ValueError):
            return Response("Invalid hours parameter", status_code=400)
        if hours <= 0 or hours > 720:
            return Response("hours must be between 0 and 720", status_code=400)
        bundle_bytes = await asyncio.to_thread(
            history.export_bundle_zip,
            _active_targets(),
            hours=hours,
            graph=cfg.graph,
        )
        return Response(
            body=bundle_bytes,
            status_code=200,
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="edge-fdd-bundle.zip"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/poll/once")
    @protect
    async def trigger_poll(request):
        samples = await poll_once()
        return {"count": len(samples), "samples": [s.to_dict() for s in samples]}

    @app.post("/sparql")
    @protect
    async def run_sparql(request):
        if not sparql.enabled:
            return Response("SPARQL disabled", status_code=503)
        body = request.json or {}
        query = body.get("query") or DEFAULT_SPARQL
        try:
            return sparql.query(query)
        except Exception as exc:
            return Response(str(exc), status_code=400)

    @app.get("/sparql/example")
    @protect
    async def sparql_example(request):
        if not sparql.enabled:
            return {"enabled": False, "example_query": DEFAULT_SPARQL}
        try:
            result = sparql.query(DEFAULT_SPARQL)
            return {"enabled": True, "example_query": DEFAULT_SPARQL, "preview": result}
        except Exception as exc:
            return {"enabled": True, "error": str(exc)}

    @app.route("/live")
    @with_session
    async def live_ws(request, session):
        if request.args.get("token"):
            return Response("Unauthorized", status_code=401)
        gate = require_token(auth, request)
        if isinstance(gate, Response):
            stored = session.get("api_access") if session is not None else None
            if not stored or auth.tokens.verify_access(str(stored)) is None:
                return gate
        ws = await websocket_upgrade(request)
        await hub.add(ws)
        try:
            snap = hot.snapshot_latest()
            if snap:
                await ws.send(json.dumps({"type": "snapshot", "count": len(snap), "samples": snap}))
            while True:
                await ws.receive()
        except WebSocketError:
            # Client closed or dropped the connection; nothing to report.
            pass
        finally:
            await hub.remove(ws)

    attach_ui_api(app, history=history, auth=auth, sparql=sparql, site=site)
    return app
