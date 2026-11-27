"""Fail when a pinned dependency in a pyproject is behind the latest PyPI release."""
from __future__ import annotations

import json
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s,\]]+)")


def pins(path: Path) -> list[tuple[str, str]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    project = data.get("project") or {}
    for dep in project.get("dependencies") or []:
        match = PIN.match(dep.strip())
        if match:
            found.append((match.group(1), match.group(2)))
    optional = project.get("optional-dependencies") or {}
    for deps in optional.values():
        for dep in deps:
            match = PIN.match(str(dep).strip())
            if match:
                found.append((match.group(1), match.group(2)))
    return found


def latest(name: str, *, attempts: int = 3, sleep=time.sleep, urlopen=None) -> str:
    """Read the current PyPI version. A dropped TLS handshake is retried."""
    opener = urlopen or urllib.request.urlopen
    url = f"https://pypi.org/pypi/{name}/json"
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            with opener(url, timeout=30) as response:
                payload = json.load(response)
            version = payload["info"]["version"]
            if not isinstance(version, str) or not version:
                raise ValueError("missing version")
            return version
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, KeyError) as exc:
            last = exc
            if attempt + 1 == attempts:
                break
            sleep(float(attempt + 1))
    raise RuntimeError(f"{name}: PyPI lookup failed") from last


def main(argv: list[str]) -> int:
    stale: list[str] = []
    for raw in argv:
        path = Path(raw)
        for name, pinned in pins(path):
            current = latest(name)
            if current != pinned:
                stale.append(f"{path}: {name}=={pinned} latest is {current}")
    if stale:
        print("\n".join(stale))
        return 1
    print("pinned dependencies match PyPI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
