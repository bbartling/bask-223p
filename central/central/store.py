"""On-disk exports: data/<site>/<edge>/<timestamp>-<sha>/."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass
class Snapshot:
    site: str
    edge: str
    name: str
    path: Path
    model_sha256: str
    received_at: str
    unchanged: bool = False


def _edge_dir(data_dir: Path, site: str, edge: str) -> Path:
    return data_dir / site / edge


def latest_model(data_dir: Path, site: str, edge: str) -> tuple[Path, dict] | None:
    pointer = _edge_dir(data_dir, site, edge) / "latest.json"
    if not pointer.is_file():
        return None
    meta = json.loads(pointer.read_text(encoding="utf-8"))
    folder = _edge_dir(data_dir, site, edge) / str(meta["snapshot"])
    model = folder / "model.ttl"
    if not model.is_file():
        return None
    return model, meta


def list_snapshots(data_dir: Path, site: str, edge: str) -> list[Path]:
    root = _edge_dir(data_dir, site, edge)
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def iter_latest(data_dir: Path) -> list[tuple[str, str, bytes]]:
    """Latest model for every stored edge. Skips ``_`` bookkeeping directories."""
    found: list[tuple[str, str, bytes]] = []
    if not data_dir.is_dir():
        return found
    for site_dir in sorted(data_dir.iterdir()):
        if not site_dir.is_dir() or site_dir.name.startswith("_"):
            continue
        for edge_dir in sorted(site_dir.iterdir()):
            if not edge_dir.is_dir() or edge_dir.name.startswith("_"):
                continue
            current = latest_model(data_dir, site_dir.name, edge_dir.name)
            if current is None:
                continue
            model, _meta = current
            found.append((site_dir.name, edge_dir.name, model.read_bytes()))
    return found


def save_snapshot(
    data_dir: Path,
    site: str,
    edge: str,
    model: bytes,
    *,
    bundle: bytes | None,
    source: str,
    keep: int,
    now: datetime | None = None,
) -> Snapshot:
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    received = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = _sha256(model)
    root = _edge_dir(data_dir, site, edge)
    root.mkdir(parents=True, exist_ok=True)
    current = latest_model(data_dir, site, edge)
    if current is not None:
        _model, meta = current
        if meta.get("model_sha256") == digest:
            return Snapshot(site, edge, str(meta["snapshot"]), root / str(meta["snapshot"]), digest, str(meta.get("received_at") or received), True)
    # Nanoseconds keep names in arrival order when several exports land in one second.
    name = f"{when.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{time.time_ns():019d}"
    folder = root / name
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "model.ttl").write_bytes(model)
    if bundle is not None:
        (folder / "export.zip").write_bytes(bundle)
    manifest = {
        "site": site,
        "edge": edge,
        "received_at": received,
        "source": source,
        "model_sha256": digest,
        "model_bytes": len(model),
        "zip_sha256": _sha256(bundle) if bundle is not None else None,
        "zip_bytes": len(bundle) if bundle is not None else None,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    pointer = {"snapshot": name, "model_sha256": digest, "received_at": received, "source": source}
    (root / "latest.json").write_text(json.dumps(pointer, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _prune(root, keep)
    return Snapshot(site, edge, name, folder, digest, received, False)


def _prune(root: Path, keep: int) -> None:
    folders = sorted(path for path in root.iterdir() if path.is_dir())
    for folder in folders[:-keep]:
        for child in folder.iterdir():
            child.unlink()
        folder.rmdir()


def load_idempotency(data_dir: Path) -> dict[str, dict[str, str]]:
    path = data_dir / "_idempotency.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    keys = payload.get("keys") if isinstance(payload, dict) else None
    return keys if isinstance(keys, dict) else {}


def store_idempotency(data_dir: Path, keys: dict[str, dict[str, str]]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "_idempotency.json"
    path.write_text(json.dumps({"keys": keys}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
