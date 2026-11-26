"""Startup note for the Open-FDD pin. Rules run only after the tag is vendored."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "OPENFDD_VERSION"
VENDOR = ROOT / "vendor" / "open-fdd"


def pinned_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def rules_zip() -> Path:
    return VENDOR / f"open-fdd-rules-{pinned_version()}.zip"


def startup_lines() -> list[str]:
    version = pinned_version()
    archive = rules_zip()
    if not archive.is_file():
        return [
            f"Open-FDD {version} is not vendored.",
            "No pinned rules are loaded. Run scripts/fetch_openfdd.py after the tag is published.",
        ]
    return [f"Open-FDD {version} rules archive: {archive.name}"]
