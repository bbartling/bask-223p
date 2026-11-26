#!/usr/bin/env python3
"""Download the pinned 223P file and write its OntoEnv import closure.

Validation stays offline. This script is the only step that fetches.
"""
from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path

from bask_223p.validate.shacl import PINNED_223P_SHA256, materialize_223p_closure

URL = "https://open223.info/223p.ttl"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args()
    args.dest.mkdir(parents=True, exist_ok=True)
    shapes = args.dest / "223p.ttl"
    with urllib.request.urlopen(URL, timeout=60) as response:
        shapes.write_bytes(response.read())
    digest = hashlib.sha256(shapes.read_bytes()).hexdigest()
    if digest != PINNED_223P_SHA256:
        raise SystemExit(f"sha256 {digest} != {PINNED_223P_SHA256}")
    names = materialize_223p_closure(shapes, args.dest / "closure")
    print(f"pinned {shapes}")
    print(f"closure {len(names)} ontologies -> {args.dest / 'closure'}")


if __name__ == "__main__":
    main()
