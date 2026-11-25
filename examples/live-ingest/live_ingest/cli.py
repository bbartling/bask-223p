"""CLI entry point for live-ingest."""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from typing import Sequence

import uvicorn

from .api import create_app
from .config import LoadedConfig, load_config
from .sinks import CsvSink, DuckDbSink, MultiSink, Sink, StdoutSink
from .sources import BacnetRpmSource, BaskStreamSource, CompositeSource


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live-ingest",
        description="Read-only live BACnet / baskStream ingest service and CLI.",
    )
    p.add_argument("--source", choices=["baskstream", "bacnet", "both"], default=None)
    p.add_argument("--points", type=Path, help="223P TTL, BACpypes3 scan TTL, CSV, or YAML config")
    p.add_argument("--site", default="demo-site", help="Site identifier written to samples")
    p.add_argument("--once", action="store_true", help="Poll once, write to sinks, and exit")
    p.add_argument("--cov", action="store_true", help="Stream baskStream COV updates instead of polling")
    p.add_argument("--duration", type=float, default=None, help="Optional max seconds for --cov or loop")
    p.add_argument("--interval", type=float, default=15.0, help="Polling interval in seconds")
    p.add_argument("--serve", action="store_true", help="Run the FastAPI server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--csv", type=Path, default=None, help="Optional CSV output path")
    p.add_argument("--duckdb", type=Path, default=None, help="Optional DuckDB file path (default var/live_ingest.duckdb with --serve)")
    p.add_argument("--no-duckdb", action="store_true", help="Disable DuckDB sink")
    p.add_argument("--stdout-json", action="store_true", help="Emit JSON lines to stdout instead of text")
    p.add_argument("--quiet", action="store_true", help="Suppress stdout sink")

    # baskStream options
    p.add_argument("--station", default=None, help="Niagara station URL, e.g. https://localhost")
    p.add_argument("--env-file", type=Path, default=None, help="Env file with NIAGARA_USER and NIAGARA_PASS")
    p.add_argument("--insecure", action="store_true", help="Skip TLS verification (localhost self-signed certs only)")
    p.add_argument("--ws-auth-header", action="store_true", help="Send HTTP Basic Authorization on WS handshake")
    p.add_argument("--base", default=None, help="Niagara discovery root ORD (default slot:/Drivers/BacnetNetwork)")

    # BACnet RPM options
    p.add_argument("--local-address", default=None, help="Local BACnet/IP bind address, e.g. 192.168.1.10/24")
    p.add_argument("--max-props", type=int, default=None, help="Max properties per ReadPropertyMultiple batch")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.points, site=args.site) if args.points else LoadedConfig(site=args.site)
    site = args.site if args.site != "demo-site" else cfg.site
    source_kind = args.source or cfg.source or "baskstream"

    sinks: list[Sink] = []
    if not args.quiet:
        sinks.append(StdoutSink(fmt="json" if args.stdout_json else "line"))
    csv_path = args.csv or (Path(cfg.csv_path) if cfg.csv_path else None)
    if csv_path:
        sinks.append(CsvSink(csv_path))

    duckdb_sink: DuckDbSink | None = None
    if not args.no_duckdb:
        ddb_path = args.duckdb or (Path(cfg.duckdb_path) if cfg.duckdb_path else None)
        if ddb_path is None and args.serve:
            ddb_path = Path("var/live_ingest.duckdb")
        if ddb_path is not None:
            duckdb_sink = DuckDbSink(ddb_path)
            sinks.append(duckdb_sink)

    sink = MultiSink(sinks)

    bs_src: BaskStreamSource | None = None
    bn_src: BacnetRpmSource | None = None
    if source_kind in {"baskstream", "both"}:
        station = args.station or cfg.station or "https://localhost"
        base_ord = args.base or cfg.base_ord
        bs_src = BaskStreamSource(
            station=station,
            targets=cfg.targets,
            env_file=args.env_file,
            insecure=args.insecure,
            send_auth_header=args.ws_auth_header,
            base_ord=base_ord,
            site=site,
        )
    if source_kind in {"bacnet", "both"}:
        if not cfg.targets:
            raise SystemExit("BACnet source requires --points (223P TTL, scan TTL, CSV, or YAML).")
        bn_src = BacnetRpmSource(
            cfg.targets,
            local_address=args.local_address or cfg.local_address,
            max_props=args.max_props or cfg.max_props,
        )

    if source_kind == "both" and bs_src and bn_src:
        source: object = CompositeSource([bs_src, bn_src])
    elif bs_src is not None:
        source = bs_src
    elif bn_src is not None:
        source = bn_src
    else:
        raise SystemExit(f"Unsupported source: {source_kind}")

    try:
        if args.serve:
            app = create_app(
                source=source,
                source_kind=source_kind,
                targets=cfg.targets,
                sinks=[s for s in sinks if s is not duckdb_sink],
                duckdb_sink=duckdb_sink,
                graph=cfg.graph,
                sidecar_graphs=cfg.sidecar_graphs(),
                poll_interval_s=args.interval if args.interval > 0 else 0.0,
                site=site,
            )
            uvicorn.run(app, host=args.host, port=args.port)
            return 0

        if args.cov:
            if bs_src is None:
                raise SystemExit("--cov requires --source baskstream or both.")
            stop_ev = threading.Event()
            try:
                bs_src.stream_cov(sink.write, stop_event=stop_ev, duration_s=args.duration)
            except KeyboardInterrupt:
                stop_ev.set()
            return 0

        if args.once:
            samples = source.poll_once()  # type: ignore[attr-defined]
            sink.write(samples)
            return 0

        deadline = (time.monotonic() + args.duration) if args.duration is not None else None
        try:
            while True:
                samples = source.poll_once()  # type: ignore[attr-defined]
                sink.write(samples)
                if deadline is not None and time.monotonic() >= deadline:
                    break
                time.sleep(max(0.1, args.interval))
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        sink.close()
