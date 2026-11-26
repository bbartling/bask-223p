"""Track OT protocol pins in ot-protocols.txt against PyPI.

    python scripts/ot_protocol_watch.py --verify
    python scripts/ot_protocol_watch.py --report
    python scripts/ot_protocol_watch.py --install-latest
    python scripts/ot_protocol_watch.py --apply

`--verify` checks every pin in ot-protocols.txt also appears, at the same
version, in a pyproject dependency list. `--report` prints JSON. Exit status
is 0 when every pin is the latest stable release, 2 when a newer release
exists, 1 on a pin mismatch or HTTP failure.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = ROOT / "ot-protocols.txt"
PYPROJECTS = (
    ROOT / "pyproject.toml",
    ROOT / "examples" / "edge-microdot" / "pyproject.toml",
    ROOT / "examples" / "live-ingest" / "pyproject.toml",
    ROOT / "examples" / "sparql-playground" / "pyproject.toml",
)
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s#]+)\s*$")
DEP = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s,\]]+)")


def parse_pins(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = PIN.match(line)
        if not match:
            raise SystemExit(f"bad ot-protocols pin: {raw}")
        found.append((match.group(1), match.group(2)))
    return found


def pyproject_pins(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    project = data.get("project") or {}
    blobs: list[str] = list(project.get("dependencies") or [])
    for deps in (project.get("optional-dependencies") or {}).values():
        blobs.extend(str(item) for item in deps)
    for dep in blobs:
        match = DEP.match(dep.strip().strip('"').strip("'"))
        if match:
            found[match.group(1)] = match.group(2)
    return found


def latest_version(name: str, opener=urllib.request.urlopen) -> str:
    url = f"https://pypi.org/pypi/{name}/json"
    with opener(url, timeout=30) as response:
        payload = json.load(response)
    return str(payload["info"]["version"])


def verify(pins: list[tuple[str, str]]) -> list[str]:
    problems: list[str] = []
    tables = [(path, pyproject_pins(path)) for path in PYPROJECTS]
    for name, pinned in pins:
        holders = [str(path.relative_to(ROOT)) for path, table in tables if table.get(name) == pinned]
        drifted = [
            f"{path.relative_to(ROOT)} pins {table[name]}"
            for path, table in tables
            if name in table and table[name] != pinned
        ]
        if drifted:
            problems.extend(drifted)
        if not holders and not any(name in table for _path, table in tables):
            problems.append(f"{name}=={pinned} is not pinned in a pyproject")
        elif not holders and drifted:
            problems.append(f"{name}=={pinned} in ot-protocols.txt does not match a pyproject")
    return problems


def report(pins: list[tuple[str, str]], opener=urllib.request.urlopen) -> dict:
    updates = []
    for name, pinned in pins:
        current = latest_version(name, opener)
        if current != pinned:
            updates.append({"name": name, "pinned": pinned, "latest": current})
    return {"updates": updates}


def apply(pins: list[tuple[str, str]], updates: list[dict]) -> None:
    latest = {row["name"]: row["latest"] for row in updates}
    lines = []
    for raw in PIN_FILE.read_text(encoding="utf-8").splitlines():
        match = PIN.match(raw.split("#", 1)[0].strip())
        if match and match.group(1) in latest:
            lines.append(f"{match.group(1)}=={latest[match.group(1)]}")
        else:
            lines.append(raw)
    PIN_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for path in PYPROJECTS:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for name, version in latest.items():
            text = re.sub(
                rf"({re.escape(name)}==)[^\s,\"']+",
                rf"\g<1>{version}",
                text,
            )
        path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch OT protocol releases on PyPI")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--install-latest", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    pins = parse_pins(PIN_FILE.read_text(encoding="utf-8"))
    if args.verify or not (args.report or args.install_latest or args.apply):
        problems = verify(pins)
        if problems:
            print("\n".join(problems))
            return 1
        if args.verify and not (args.report or args.install_latest or args.apply):
            print("ot-protocols.txt matches pyproject pins")
            return 0
    body = report(pins)
    if args.report or not (args.install_latest or args.apply):
        json.dump(body, sys.stdout, indent=2)
        sys.stdout.write("\n")
    if args.install_latest:
        import subprocess

        spec = []
        for name, pinned in pins:
            version = pinned
            for row in body["updates"]:
                if row["name"] == name:
                    version = row["latest"]
            spec.append(f"{name}=={version}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *spec])
    if args.apply:
        apply(pins, body["updates"])
    return 2 if body["updates"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
