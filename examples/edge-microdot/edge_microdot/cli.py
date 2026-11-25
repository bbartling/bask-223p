"""CLI for edge-microdot."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Sequence

from edge_microdot.app import create_app
from edge_microdot.cache import HotCache
from edge_microdot.config import load_config
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.logging_setup import configure_logging
from edge_microdot.models import PointTarget
from edge_microdot.sources import BacnetRpmSource, BaskStreamSource
from edge_microdot.sources.fake import FakePollSource
from edge_microdot.sparql_kb import SparqlKb


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="edge-microdot", description="Edge BACnet / Niagara ingest (Microdot)")
    p.add_argument("--driver", choices=["bacnet", "niagara", "fake"], default=None)
    p.add_argument("--points", type=Path, help="223P TTL, scan TTL, CSV, or YAML config")
    p.add_argument("--site", default=None)
    p.add_argument("--station", default=None, help="Niagara URL (or NIAGARA_URL)")
    p.add_argument("--base", default=None, help="Niagara browse root ORD")
    p.add_argument("--local-address", default=None)
    p.add_argument("--max-props", type=int, default=None)
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--poll-interval", type=float, default=None)
    p.add_argument("--flush-interval", type=float, default=None)
    p.add_argument("--fdd-interval", type=float, default=None)
    p.add_argument("--db", type=Path, default=None, help="DuckDB history file path")
    p.add_argument("--db-max-mb", type=int, default=None)
    p.add_argument("--hot-backend", choices=["dict", "duckdb"], default=None)
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--ws-auth-header", action="store_true")
    p.add_argument("--fake-points", type=int, default=50, help="Fake driver point count for demos/tests")
    p.add_argument("--once", action="store_true", help="Poll once and exit (no server)")
    return p


def _resolve_driver(args: argparse.Namespace) -> str:
    return (args.driver or os.environ.get("EDGE_DRIVER") or "fake").strip().lower()


def _build_targets_fake(count: int, site: str) -> list[PointTarget]:
    targets: list[PointTarget] = []
    for i in range(1, count + 1):
        dev = 10001
        obj = f"analog-input,{i}"
        targets.append(
            PointTarget(
                site=site,
                device=str(dev),
                object_id=obj,
                units="C",
                label=f"AI-{i}",
            )
        )
    return targets


def build_runtime(argv: Sequence[str] | None = None) -> tuple:
    configure_logging()
    args = build_parser().parse_args(argv)
    site = args.site or os.environ.get("EDGE_SITE", "demo-site")
    driver = _resolve_driver(args)
    cfg = load_config(args.points, site=site) if args.points else None
    if cfg is None:
        from edge_microdot.config import LoadedConfig

        cfg = LoadedConfig(site=site)

    targets = cfg.targets
    if driver == "fake" and not targets:
        targets = _build_targets_fake(args.fake_points, site)

    if driver in {"bacnet", "niagara"} and not targets and args.points is None:
        raise SystemExit(f"{driver} driver requires --points or preconfigured targets")

    source_kind = driver
    if driver == "fake":
        source = FakePollSource(targets)
    elif driver == "bacnet":
        source = BacnetRpmSource(
            targets,
            local_address=args.local_address or cfg.local_address,
            max_props=args.max_props or cfg.max_props,
        )
    elif driver == "niagara":
        source = BaskStreamSource(
            station=args.station,
            targets=targets,
            insecure=args.insecure or _env_bool("NIAGARA_INSECURE"),
            send_auth_header=args.ws_auth_header or _env_bool("NIAGARA_WS_AUTH_HEADER"),
            base_ord=args.base or cfg.base_ord,
            site=site,
        )
    else:
        raise SystemExit(f"Unknown driver: {driver}")

    hot_backend = args.hot_backend or os.environ.get("EDGE_HOT_BACKEND", "dict")
    hot = HotCache(backend=hot_backend)  # type: ignore[arg-type]
    db_path = args.db or Path(os.environ.get("EDGE_DB_PATH", "var/edge_history.duckdb"))
    max_mb = args.db_max_mb or int(os.environ.get("EDGE_DB_MAX_MB", "100"))
    history = HistoryStore(db_path, max_bytes=max_mb * 1024 * 1024)
    fdd = FddEngine(history=history)
    enable_sparql = _env_bool("EDGE_ENABLE_SPARQL") or bool(cfg.graph and len(cfg.graph) > 0)
    if not enable_sparql:
        sparql = SparqlKb(None)
    else:
        sparql = SparqlKb(cfg.graph)

    poll_interval = args.poll_interval or float(os.environ.get("EDGE_POLL_INTERVAL", "15"))
    flush_interval = args.flush_interval or float(os.environ.get("EDGE_FLUSH_INTERVAL", "30"))
    fdd_interval = args.fdd_interval or float(os.environ.get("EDGE_FDD_INTERVAL", "60"))
    host = args.host or os.environ.get("EDGE_HOST", "0.0.0.0")
    port = args.port or int(os.environ.get("EDGE_PORT", "8080"))

    app = create_app(
        source=source,
        source_kind=source_kind,
        targets=targets,
        cfg=cfg,
        hot=hot,
        history=history,
        fdd=fdd,
        sparql=sparql,
        site=site,
        poll_interval_s=poll_interval,
        flush_interval_s=flush_interval,
        fdd_interval_s=fdd_interval,
    )
    return app, source, hot, history, host, port, args


def main(argv: Sequence[str] | None = None) -> int:
    app, source, hot, history, host, port, args = build_runtime(argv)
    try:
        if args.once:

            async def _once() -> None:
                if hasattr(source, "poll_once_async"):
                    samples = await source.poll_once_async()
                else:
                    samples = source.poll_once()
                hot.write(samples)
                history.stage(samples)
                history.flush()
                for s in samples:
                    print(s.to_dict())

            asyncio.run(_once())
            return 0
        app.start_background_workers()
        app.run(host=host, port=port, debug=False)
        return 0
    finally:
        history.close()
        hot.close()
        if hasattr(source, "close"):
            asyncio.run(source.close())


if __name__ == "__main__":
    raise SystemExit(main())