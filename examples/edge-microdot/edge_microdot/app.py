"""Microdot HTTP API for edge ingest."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

from microdot import Microdot, Response, send_file
from microdot.session import with_session
from microdot.websocket import WebSocketError, websocket_upgrade

from edge_microdot.cache import HotCache
from edge_microdot.config import LoadedConfig
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointSample, PointTarget
from edge_microdot.publish import publish_enabled, scheduled_publish
from edge_microdot.security import AuthBundle, api_auth, attach_auth_routes, require_token
from edge_microdot.share import attach_share_api
from edge_microdot.sparql_kb import SparqlKb
from edge_microdot.bacnet_api import attach_bacnet_api
from edge_microdot.modbus_api import attach_modbus_api
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
    """Fan out live samples on the server loop without waiting on a slow client.

    Each socket has a bounded queue. A full queue means the client is behind,
    so that client is dropped. ``max_clients`` caps how many sockets stay open.
    """

    def __init__(self, *, max_clients: int = 32, queue_size: int = 8) -> None:
        self._clients: dict[Any, asyncio.Queue[str]] = {}
        self._tasks: dict[Any, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self.max_clients = max(1, max_clients)
        self.queue_size = max(1, queue_size)

    async def add(self, ws: Any) -> bool:
        async with self._lock:
            if len(self._clients) >= self.max_clients:
                return False
            queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.queue_size)
            self._clients[ws] = queue
            self._tasks[ws] = asyncio.create_task(self._pump(ws, queue))
            return True

    async def remove(self, ws: Any) -> None:
        async with self._lock:
            self._clients.pop(ws, None)
            task = self._tasks.pop(ws, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def count(self) -> int:
        async with self._lock:
            return len(self._clients)

    async def _pump(self, ws: Any, queue: asyncio.Queue[str]) -> None:
        try:
            while True:
                message = await queue.get()
                await ws.send(message)
        except Exception:
            await self.remove(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        message = json.dumps(payload)
        async with self._lock:
            clients = list(self._clients.items())
        for ws, queue in clients:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                await self.remove(ws)


class _StreamedFile:
    """File body that Microdot reads in chunks and deletes when the response ends."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh = path.open("rb")

    def read(self, n: int = -1) -> bytes:
        return self._fh.read(n)

    def close(self) -> None:
        self._fh.close()
        self._path.unlink(missing_ok=True)


def tls_name_allowed(name: str) -> bool:
    """True when Caddy may issue a certificate for this SNI or IP.

    ``EDGE_TLS_ALLOW`` is the list. If it is unset, ``EDGE_TLS_HOST`` is used.
    The catch-all site address ``:443`` is not an allow entry, so on-demand
    issuance stays closed until names are configured.
    """
    raw = os.environ.get("EDGE_TLS_ALLOW", "").strip() or os.environ.get("EDGE_TLS_HOST", "")
    allowed: set[str] = set()
    for part in raw.split(","):
        item = part.strip().lower().rstrip(".")
        if not item or item in {":443", "443", "*"} or item.startswith(":"):
            continue
        allowed.add(item)
    candidate = name.strip().lower().rstrip(".")
    return bool(candidate) and candidate in allowed


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
    hub = LiveHub(
        max_clients=int(os.environ.get("EDGE_LIVE_CLIENTS", "32")),
        queue_size=int(os.environ.get("EDGE_LIVE_QUEUE", "8")),
    )
    targets_list = targets if isinstance(targets, list) else list(targets)
    configured_targets = list(targets_list)
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

    async def poll_once() -> list[PointSample]:
        async with poll_lock:
            if hasattr(source, "poll_once_async"):
                samples = await source.poll_once_async()
            else:
                samples = await asyncio.to_thread(source.poll_once)
            if samples:
                hot.write(samples)
                history.stage(samples)
        if samples:
            await hub.broadcast(
                {"type": "samples", "count": len(samples), "samples": [s.to_dict() for s in samples]}
            )
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
                def _flush() -> int:
                    n = history.flush()
                    history.prune_memory()
                    return n

                n = await asyncio.to_thread(_flush)
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
                await asyncio.to_thread(fdd.run, targets=_active_targets())
            except Exception:
                LOG.exception("fdd failed")

    def start_background_workers() -> list[asyncio.Task[None]]:
        """Start poll, flush, snapshot, and FDD tasks on the running loop."""
        tasks = [
            asyncio.create_task(poll_loop()),
            asyncio.create_task(flush_loop()),
            asyncio.create_task(snapshot_loop()),
            asyncio.create_task(fdd_loop()),
        ]
        if publish_enabled():
            tasks.append(
                asyncio.create_task(
                    scheduled_publish(
                        cfg,
                        configured_targets,
                        sparql.store if sparql.enabled else None,
                    )
                )
            )
        return tasks

    async def serve(host: str = "127.0.0.1", port: int = 8080, ssl: Any = None) -> None:
        tasks = start_background_workers()
        try:
            await app.start_server(host=host, port=port, debug=False, ssl=ssl)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app.start_background_workers = start_background_workers  # type: ignore[attr-defined]
    app.serve = serve  # type: ignore[attr-defined]

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

    @app.get("/tls/allow")
    async def tls_allow(request):
        domain = str(request.args.get("domain") or "")
        if tls_name_allowed(domain):
            return Response("", status_code=200)
        return Response("", status_code=403)

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
        bundle_path = await asyncio.to_thread(
            history.export_bundle_file,
            _active_targets(),
            hours=hours,
            graph=cfg.graph,
        )
        return Response(
            body=_StreamedFile(bundle_path),
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
        origin = request.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            host = request.headers.get("Host") or ""
            if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != host.lower():
                return Response("Forbidden", status_code=403)
        gate = require_token(auth, request)
        if isinstance(gate, Response):
            stored = session.get("api_access") if session is not None else None
            if not stored or auth.tokens.verify_access(str(stored)) is None:
                return gate
        ws = await websocket_upgrade(request)
        if not await hub.add(ws):
            await ws.close()
            return
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

    attach_bacnet_api(app, source=source, auth=auth)
    attach_modbus_api(app, source=source, auth=auth)
    attach_ui_api(app, history=history, auth=auth, sparql=sparql, site=site)
    attach_share_api(
        app,
        auth=auth,
        cfg=cfg,
        targets=configured_targets,
        graph=cfg.graph,
        store=sparql.store if sparql.enabled else None,
    )
    return app
