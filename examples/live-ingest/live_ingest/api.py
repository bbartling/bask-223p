"""Unified FastAPI service: polling, WebSocket /live, DuckDB history & SQL, pyoxigraph SPARQL, and BACnet discovery."""
from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path
from typing import Any, Sequence

from fastapi import FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from rdflib import Graph

from .models import PointSample, PointTarget
from .playground import PLAYGROUND_HTML
from .rdf_store import RdfKnowledgeBase
from .sinks import DuckDbSink, MemoryRingSink, MultiSink, Sink
from .sources import BacnetRpmSource, CompositeSource


DEFAULT_JOIN_SPARQL = """PREFIX s223: <http://data.ashrae.org/standard223#>
PREFIX ref: <https://brickschema.org/schema/Brick/ref#>
PREFIX qudt: <http://qudt.org/schema/qudt/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?point ?label ?ref ?unit WHERE {
  { ?point s223:hasExternalReference ?r } UNION { ?point ref:hasExternalReference ?r }
  ?r ref:BACnetURI ?ref .
  OPTIONAL { ?point rdfs:label ?label }
  OPTIONAL { ?point qudt:hasUnit ?unit }
}
ORDER BY ?ref"""


class SparqlRequest(BaseModel):
    query: str = Field(..., description="Read-only SPARQL SELECT, ASK, CONSTRUCT, or DESCRIBE query.")


class SqlRequest(BaseModel):
    sql: str = Field(..., description="Read-only DuckDB SQL query over the telemetry table.")


class ReadMultipleItem(BaseModel):
    device: int
    object_id: str
    property_id: str = "present-value"


class ReadMultipleRequest(BaseModel):
    items: list[ReadMultipleItem]


class _WsHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def count(self) -> int:
        async with self._lock:
            return len(self._clients)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._clients)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


def _find_bacnet_source(source: Any) -> BacnetRpmSource | None:
    if isinstance(source, BacnetRpmSource):
        return source
    if isinstance(source, CompositeSource):
        for sub in source.sources:
            if isinstance(sub, BacnetRpmSource):
                return sub
    return None


def _load_query_presets() -> list[dict[str, str]]:
    qdir = Path(__file__).resolve().parents[1] / "queries"
    presets: list[dict[str, str]] = []
    if qdir.is_dir():
        for f in sorted(qdir.glob("*.rq")):
            presets.append({"name": f.stem, "query": f.read_text(encoding="utf-8")})
    if not presets:
        presets.append({"name": "default_223p_refs", "query": DEFAULT_JOIN_SPARQL})
    return presets


def create_app(
    *,
    source: Any,
    source_kind: str,
    targets: Sequence[PointTarget],
    sinks: Sequence[Sink] = (),
    duckdb_sink: DuckDbSink | None = None,
    graph: Graph | None = None,
    poll_interval_s: float = 0.0,
    site: str = "demo-site",
) -> FastAPI:
    ring = MemoryRingSink()
    sink_list: list[Sink] = [ring]
    if duckdb_sink is not None and duckdb_sink not in sinks:
        sink_list.append(duckdb_sink)
    sink_list.extend(sinks)
    all_sinks = MultiSink(sink_list)
    kb = RdfKnowledgeBase(graph)
    ws_hub = _WsHub()
    targets_list = list(targets)
    poll_lock = asyncio.Lock()
    bacnet_src = _find_bacnet_source(source)

    async def _run_poll_once() -> list[PointSample]:
        async with poll_lock:
            if isinstance(source, (BacnetRpmSource, CompositeSource)):
                samples = await source.poll_once_async()
            else:
                samples = await asyncio.to_thread(source.poll_once)
            all_sinks.write(samples)
            if samples:
                await ws_hub.broadcast({
                    "type": "samples",
                    "count": len(samples),
                    "samples": [s.to_dict() for s in samples],
                })
            return samples

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop_event = threading.Event()
        bg_task: asyncio.Task[None] | None = None

        async def _poll_loop() -> None:
            while not stop_event.is_set():
                try:
                    await _run_poll_once()
                except Exception:
                    pass
                await asyncio.sleep(poll_interval_s)

        if poll_interval_s and poll_interval_s > 0:
            bg_task = asyncio.create_task(_poll_loop())
        try:
            yield
        finally:
            stop_event.set()
            if bg_task is not None:
                bg_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bg_task
            if isinstance(source, (BacnetRpmSource, CompositeSource)):
                await source.close()
            all_sinks.close()

    app = FastAPI(
        title="live-ingest",
        version="0.1.0",
        description="Unified read-only BACnet / baskStream telemetry ingest service with pyoxigraph SPARQL, OntoEnv/Shifty, and DuckDB.",
        lifespan=lifespan,
    )

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/playground")

    @app.get("/playground", response_class=HTMLResponse, tags=["playground"])
    async def playground_page() -> HTMLResponse:
        return HTMLResponse(PLAYGROUND_HTML)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "site": site,
            "source": source_kind,
            "point_count": len(targets_list),
            "latest_count": len(ring.latest),
            "last_updated_utc": ring.last_updated_utc,
            "ws_clients": await ws_hub.count(),
            "graph_triples": kb.triple_count,
            "sparql_engine": "pyoxigraph",
            "duckdb_enabled": duckdb_sink is not None,
        }

    @app.get("/points", tags=["telemetry"])
    async def list_points(
        device: str | None = Query(default=None),
        object_type: str | None = Query(default=None),
    ) -> dict[str, Any]:
        out = []
        for t in targets_list:
            if device and t.device != device:
                continue
            if object_type and not t.object_id.startswith(f"{object_type},"):
                continue
            out.append(t.to_dict())
        return {"count": len(out), "points": out}

    @app.post("/poll/once", tags=["telemetry"])
    async def poll_once() -> dict[str, Any]:
        samples = await _run_poll_once()
        return {
            "count": len(samples),
            "samples": [s.to_dict() for s in samples],
        }

    @app.get("/latest", tags=["telemetry"])
    async def get_latest() -> dict[str, Any]:
        rows = ring.snapshot_latest()
        return {"count": len(rows), "samples": rows}

    @app.get("/history", tags=["telemetry"])
    async def get_history(
        ref: str | None = Query(default=None, description="bacnet://<device>/<type>,<instance>"),
        start: str | None = Query(default=None, description="Inclusive ISO-8601 UTC start timestamp"),
        end: str | None = Query(default=None, description="Inclusive ISO-8601 UTC end timestamp"),
        limit: int = Query(default=200, ge=1, le=5000),
    ) -> dict[str, Any]:
        if duckdb_sink is not None:
            try:
                rows = duckdb_sink.query_history(ref=ref, start=start, end=end, limit=limit)
                return {"store": "duckdb", "ref": ref, "count": len(rows), "samples": rows}
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"DuckDB history query failed: {exc}") from exc
        rows = ring.snapshot_history(ref=ref, start=start, end=end, limit=limit)
        return {"store": "memory", "ref": ref, "count": len(rows), "samples": rows}

    @app.websocket("/live")
    async def live_ws(ws: WebSocket) -> None:
        await ws_hub.connect(ws)
        try:
            latest = ring.snapshot_latest()
            if latest:
                await ws.send_json({"type": "snapshot", "count": len(latest), "samples": latest})
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await ws_hub.disconnect(ws)

    @app.get("/bacnet/who-is", tags=["bacnet"])
    async def bacnet_who_is(
        low_limit: int | None = Query(default=None, ge=0, le=4194303),
        high_limit: int | None = Query(default=None, ge=0, le=4194303),
    ) -> dict[str, Any]:
        if bacnet_src is None:
            raise HTTPException(status_code=400, detail="Who-Is requires --source bacnet or --source both")
        devices = await bacnet_src.who_is(low_limit=low_limit, high_limit=high_limit)
        return {
            "count": len(devices),
            "devices": [
                {
                    "device_instance": d.device_instance,
                    "address": d.address,
                    "max_apdu_length_accepted": d.max_apdu_length_accepted,
                    "segmentation_supported": d.segmentation_supported,
                    "vendor_id": d.vendor_id,
                }
                for d in devices
            ],
        }

    @app.get("/bacnet/devices/{device_id}/objects", tags=["bacnet"])
    async def bacnet_device_objects(device_id: int) -> dict[str, Any]:
        if bacnet_src is None:
            raise HTTPException(status_code=400, detail="Object discovery requires --source bacnet or --source both")
        try:
            objects = await bacnet_src.discover_objects(device_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"device_instance": device_id, "count": len(objects), "objects": objects}

    @app.post("/bacnet/read-multiple", tags=["bacnet"])
    async def bacnet_read_multiple(req: ReadMultipleRequest) -> dict[str, Any]:
        if bacnet_src is None:
            raise HTTPException(status_code=400, detail="read-multiple requires --source bacnet or --source both")
        temp_targets = [
            PointTarget(
                site=site,
                device=str(it.device),
                object_id=it.object_id,
                property_id=it.property_id,
            )
            for it in req.items
        ]
        temp_src = BacnetRpmSource(
            temp_targets,
            transport=bacnet_src.transport,
            max_props=bacnet_src.max_props,
        )
        temp_src.device_cache.update(bacnet_src.device_cache)
        samples = await temp_src.poll_once_async()
        return {"count": len(samples), "samples": [s.to_dict() for s in samples]}

    @app.get("/api/sparql/examples", tags=["sparql"])
    async def sparql_examples() -> dict[str, Any]:
        return {"examples": _load_query_presets()}

    def _exec_sparql(query_text: str) -> dict[str, Any]:
        if kb.triple_count == 0:
            raise HTTPException(status_code=404, detail="No RDF/Turtle graph loaded (pass --points path/to/model.ttl)")
        try:
            return kb.query(query_text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"SPARQL error: {exc}") from exc

    @app.post("/api/sparql", tags=["sparql"])
    @app.post("/sparql", include_in_schema=False)
    async def run_sparql(req: SparqlRequest) -> dict[str, Any]:
        return _exec_sparql(req.query)

    @app.post("/api/sparql/upload", tags=["sparql"])
    async def upload_sparql(file: UploadFile = File(...)) -> dict[str, Any]:
        raw = await file.read()
        if len(raw) > 200_000:
            raise HTTPException(status_code=400, detail="file too large")
        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="file must be UTF-8 text") from exc
        return _exec_sparql(text)

    @app.get("/api/model/ttl", tags=["sparql"])
    @app.get("/model/ttl", include_in_schema=False)
    async def get_model_ttl() -> PlainTextResponse:
        if kb.triple_count == 0:
            raise HTTPException(status_code=404, detail="No RDF/Turtle graph loaded")
        return PlainTextResponse(kb.serialize_turtle(), media_type="text/turtle")

    @app.post("/api/model/validate", tags=["sparql"])
    async def validate_model(shapes_path: str | None = Query(default=None)) -> dict[str, Any]:
        if kb.triple_count == 0:
            raise HTTPException(status_code=404, detail="No RDF/Turtle graph loaded")
        sp = Path(shapes_path) if shapes_path else None
        return kb.validate(shapes_path=sp)

    @app.post("/api/sql", tags=["duckdb"])
    @app.post("/sql", include_in_schema=False)
    async def run_sql(req: SqlRequest) -> dict[str, Any]:
        if duckdb_sink is None:
            raise HTTPException(status_code=404, detail="DuckDB sink is disabled")
        try:
            return duckdb_sink.query_sql(req.sql)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"DuckDB SQL error: {exc}") from exc

    def _run_sparql_duckdb_join(query_text: str) -> dict[str, Any]:
        if duckdb_sink is None:
            raise HTTPException(status_code=404, detail="DuckDB sink is disabled")
        sp_res = _exec_sparql(query_text)
        if sp_res.get("type") != "SELECT":
            raise HTTPException(status_code=400, detail="Join requires a SELECT SPARQL query returning ?ref")
        rows = sp_res.get("rows") or []
        refs = [r.get("ref") for r in rows if r.get("ref")]
        stats_by_ref: dict[str, dict[str, Any]] = {}
        if refs:
            placeholders = ", ".join(["?"] * len(refs))
            sql = f"""
                SELECT
                    ref,
                    COUNT(*) AS sample_count,
                    ROUND(AVG(value_num), 3) AS avg_value,
                    MIN(value_num) AS min_value,
                    MAX(value_num) AS max_value,
                    ARG_MAX(COALESCE(CAST(value_num AS VARCHAR), value_text), timestamp_utc) AS last_value,
                    ARG_MAX(status, timestamp_utc) AS last_status,
                    strftime(MAX(timestamp_utc) AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%gZ') AS last_timestamp_utc
                FROM telemetry
                WHERE ref IN ({placeholders})
                GROUP BY ref
            """
            with duckdb_sink._lock:
                cur = duckdb_sink.conn.execute(sql, refs)
                scols = [d[0] for d in cur.description]
                for srow in cur.fetchall():
                    rec = dict(zip(scols, srow))
                    stats_by_ref[rec["ref"]] = rec

        joined_rows: list[dict[str, Any]] = []
        extra_cols = [
            "sample_count",
            "avg_value",
            "min_value",
            "max_value",
            "last_value",
            "last_status",
            "last_timestamp_utc",
        ]
        for r in rows:
            ref_val = r.get("ref") or ""
            st = stats_by_ref.get(ref_val, {})
            merged = dict(r)
            for c in extra_cols:
                merged[c] = st.get(c)
            joined_rows.append(merged)

        return {
            "sparql_engine": sp_res.get("engine"),
            "columns": list(sp_res.get("columns") or []) + extra_cols,
            "count": len(joined_rows),
            "rows": joined_rows,
        }

    @app.post("/api/join", tags=["duckdb"])
    async def sparql_duckdb_join(req: SparqlRequest) -> dict[str, Any]:
        return _run_sparql_duckdb_join(req.query)

    @app.get("/api/join/example", tags=["duckdb"])
    async def sparql_duckdb_join_example() -> dict[str, Any]:
        return _run_sparql_duckdb_join(DEFAULT_JOIN_SPARQL)

    return app
