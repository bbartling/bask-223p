"""Download pinned Open-FDD release assets. No network at runtime.

OPENFDD_VERSION names the tag (v<version> on GitHub). This script verifies
sha256 and writes the zips under vendor/open-fdd/. It does not run from the
edge process. If the tag has no release yet, it exits 0 unless --require.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "OPENFDD_VERSION"
VENDOR = ROOT / "vendor" / "open-fdd"
REPO = "bbartling/open-fdd"
ASSETS = ("open-fdd-rules-{version}.zip", "open-fdd-ui-{version}.zip")


def version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        dest.write_bytes(response.read())


def fetch(*, require: bool) -> int:
    ver = version()
    missing: list[str] = []
    for name in ASSETS:
        filename = name.format(version=ver)
        base = f"https://github.com/{REPO}/releases/download/v{ver}/{filename}"
        zip_path = VENDOR / filename
        try:
            _download(base, zip_path)
            _download(base + ".sha256", VENDOR / (filename + ".sha256"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                missing.append(filename)
                continue
            raise
        expected = (VENDOR / (filename + ".sha256")).read_text(encoding="utf-8").split()[0]
        got = _sha256(zip_path)
        if got != expected:
            raise SystemExit(f"{filename} sha256 {got} != {expected}")
        print(f"vendored {filename}")
    if missing:
        message = f"Open-FDD v{ver} has no release asset yet: {', '.join(missing)}"
        if require:
            raise SystemExit(message)
        print(message)
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require", action="store_true", help="Fail if the tag is not published")
    args = parser.parse_args(argv)
    return fetch(require=args.require)


if __name__ == "__main__":
    raise SystemExit(main())
