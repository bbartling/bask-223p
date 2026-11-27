#!/usr/bin/env python3
"""Copy the pinned 223P file and its vendored OntoEnv import closure.

Nothing is downloaded. The closure was resolved once with OntoEnv; this step
checks the pins and writes those files for the offline validator.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from bask_223p.validate.shacl import PINNED_223P_SHA256, materialize_223p_closure, vendored_223p_ttl


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args()
    args.dest.mkdir(parents=True, exist_ok=True)
    shapes = args.dest / "223p.ttl"
    payload = vendored_223p_ttl().read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != PINNED_223P_SHA256:
        raise SystemExit(f"sha256 {digest} != {PINNED_223P_SHA256}")
    shapes.write_bytes(payload)
    names = materialize_223p_closure(shapes, args.dest / "closure")
    print(f"pinned {shapes}")
    print(f"closure {len(names)} ontologies -> {args.dest / 'closure'}")


if __name__ == "__main__":
    main()
