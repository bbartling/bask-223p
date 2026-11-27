"""``python -m central --config central.yaml``"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .api import serve
from .bind import resolve_interface
from .config import ConfigError, load_config
from .service import Collector, env_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m central",
        description="Collect 223P exports from edges. Does not connect to controllers or stations.",
    )
    parser.add_argument("--config", required=True, help="YAML file. Tokens are environment variable names")
    parser.add_argument("--bind", default=None, help="LAN interface name or address. Overrides config interface")
    parser.add_argument("--port", type=int, default=None, help="TCP port. Default 8780")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        cfg = load_config(args.config)
        if args.bind:
            cfg.interface = args.bind
        if args.port:
            cfg.port = args.port
        cfg.host = resolve_interface(cfg.interface)
        env_token(cfg.token_env, os.environ)
    except (ConfigError, ValueError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    collector = Collector(cfg)
    collector.rebuild()
    collector.start_schedule()
    serve(collector)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
