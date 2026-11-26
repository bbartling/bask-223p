"""CLI for edge-microdot."""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Sequence

from edge_microdot.app import create_app
from edge_microdot.cache import HotCache
from edge_microdot.config import load_config, resolve_bacnet_local_address
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.logging_setup import configure_logging
from edge_microdot.models import PointTarget
from edge_microdot.sources import BacnetRpmSource, BaskStreamSource
from edge_microdot.sources.fake import FakePollSource
from edge_microdot.security import load_auth_from_env
from edge_microdot.sparql_kb import SparqlKb


def _load_ssl_context():
    cert = os.environ.get("EDGE_SSL_CERT", "").strip()
    key = os.environ.get("EDGE_SSL_KEY", "").strip()
    if not cert or not key:
        return None
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


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
    p.add_argument("--local-address", "--address", dest="local_address", default=None,
                   help="BACpypes3 bind address, e.g. 192.168.204.59/24 (or EDGE_BACNET_ADDRESS)")
    p.add_argument("--devices", default=None,
                   help="Comma-separated BACnet device instances for Who-Is discovery")
    p.add_argument("--max-props", type=int, default=None)
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--poll-interval", type=float, default=None)
    p.add_argument("--flush-interval", type=float, default=None)
    p.add_argument("--fdd-interval", type=float, default=None)
    p.add_argument("--snapshot-interval", type=float, default=None)
    p.add_argument("--retention-hours", type=float, default=None)
    p.add_argument("--db", type=Path, default=None, help="DuckDB history file path")
    p.add_argument("--db-max-mb", type=int, default=None)
    p.add_argument("--hot-backend", choices=["dict", "duckdb"], default=None)
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--ws-auth-header", action="store_true")
    p.add_argument("--fake-points", type=int, default=50, help="Fake driver point count for demos/tests")
    p.add_argument("--once", action="store_true", help="Poll once and exit (no server)")
    return p



def _device_instances(args: argparse.Namespace) -> list[int]:
    raw = args.devices or os.environ.get("EDGE_BACNET_DEVICES") or ""
    out: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


def _sparql_enabled(cfg) -> bool:
    if os.environ.get("EDGE_ENABLE_SPARQL") is not None:
        return _env_bool("EDGE_ENABLE_SPARQL")
    return bool(cfg.graph and len(cfg.graph) > 0)


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

    source_kind = driver
    if driver == "fake":
        source = FakePollSource(targets)
    elif driver == "bacnet":
        address = resolve_bacnet_local_address(cli=args.local_address, config=cfg.local_address)
        instances = _device_instances(args)
        if not targets and not instances:
            raise SystemExit(
                "bacnet driver requires --points or EDGE_BACNET_DEVICES / --devices "
                "(comma-separated device instances to discover via Who-Is)"
            )
        # Discover on the poll loop, not a throwaway asyncio.run() here.
        # BACpypes3 Application is loop-bound; a second loop would hang Who-Is/RPM.
        source = BacnetRpmSource(
            targets,
            local_address=address,
            max_props=args.max_props or cfg.max_props,
            discover_instances=instances if not targets else (),
            site=site,
        )
    elif driver == "niagara":
        source = BaskStreamSource(
            station=args.station,
            targets=targets,
            insecure=args.insecure or _env_bool("NIAGARA_INSECURE"),
            send_auth_header=args.ws_auth_header or _env_bool("NIAGARA_WS_AUTH_HEADER"),
            base_ord=args.base or os.environ.get("BASK_BASE_ORD") or cfg.base_ord,
            site=site,
        )
    else:
        raise SystemExit(f"Unknown driver: {driver}")

    hot_backend = args.hot_backend or os.environ.get("EDGE_HOT_BACKEND", "dict")
    hot = HotCache(backend=hot_backend)  # type: ignore[arg-type]
    db_path = args.db or Path(os.environ.get("EDGE_DB_PATH", "var/edge_history.duckdb"))
    max_mb = args.db_max_mb or int(os.environ.get("EDGE_DB_MAX_MB", "100"))
    retention_hours = args.retention_hours or float(os.environ.get("EDGE_MEMORY_RETENTION_HOURS", "48"))
    history = HistoryStore(db_path, max_bytes=max_mb * 1024 * 1024, retention_hours=retention_hours)
    fdd = FddEngine(history=history, targets=targets)
    enable_sparql = _sparql_enabled(cfg)
    if not enable_sparql:
        sparql = SparqlKb(None)
    else:
        sparql = SparqlKb(cfg.graph)

    poll_interval = args.poll_interval or float(os.environ.get("EDGE_POLL_INTERVAL", "15"))
    flush_interval = args.flush_interval or float(os.environ.get("EDGE_FLUSH_INTERVAL", "30"))
    fdd_interval = args.fdd_interval or float(os.environ.get("EDGE_FDD_INTERVAL", "60"))
    snapshot_interval = args.snapshot_interval or float(os.environ.get("EDGE_SNAPSHOT_INTERVAL", "300"))
    host = args.host or os.environ.get("EDGE_HOST", "0.0.0.0")
    port = args.port or int(os.environ.get("EDGE_PORT", "8080"))

    auth = load_auth_from_env()
    app = create_app(
        source=source,
        source_kind=source_kind,
        targets=targets,
        cfg=cfg,
        hot=hot,
        history=history,
        fdd=fdd,
        sparql=sparql,
        auth=auth,
        site=site,
        poll_interval_s=poll_interval,
        flush_interval_s=flush_interval,
        fdd_interval_s=fdd_interval,
        snapshot_interval_s=snapshot_interval,
    )
    return app, source, hot, history, host, port, args


def main(argv: Sequence[str] | None = None) -> int:
    app, source, hot, history, host, port, args = build_runtime(argv)
    source_closed = False
    try:
        if args.once:

            async def _once() -> None:
                nonlocal source_closed
                if hasattr(source, "poll_once_async"):
                    samples = await source.poll_once_async()
                else:
                    samples = source.poll_once()
                hot.write(samples)
                history.stage(samples)
                history.flush()
                history.write_snapshot()
                for s in samples:
                    print(s.to_dict())
                if hasattr(source, "close"):
                    await source.close()
                    source_closed = True

            asyncio.run(_once())
            return 0
        def _on_sigterm(_signum, _frame):
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _on_sigterm)
        app.start_background_workers()
        ssl_ctx = _load_ssl_context()
        app.run(host=host, port=port, debug=False, ssl=ssl_ctx)
        return 0
    finally:
        history.close()
        hot.close()
        if hasattr(source, "close") and not source_closed:
            try:
                asyncio.run(source.close())
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())