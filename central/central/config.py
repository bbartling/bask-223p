"""Load the collector config. Tokens are environment variable names, never values."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_KEYS = {"token", "password", "secret", "authorization", "bearer"}


class ConfigError(ValueError):
    """The config file is not usable."""


@dataclass
class EdgeConfig:
    name: str
    site: str
    base_url: str
    token_env: str
    tls_verify: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return (self.site, self.name)


@dataclass
class CentralConfig:
    edges: list[EdgeConfig]
    data_dir: Path
    keep: int = 8
    interval_s: float = 3600.0
    port: int = 8780
    interface: str = "Ethernet"
    token_env: str = "CENTRAL_TOKEN"
    sparql: bool = False
    max_upload_bytes: int = 32 * 1024 * 1024
    pull_attempts: int = 3
    pull_backoff_s: float = 1.0
    shapes: Path | None = None
    ontology_dir: Path | None = None
    host: str = ""
    config_dir: Path = field(default_factory=Path.cwd)


def _reject_secrets(value: Any, where: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in _SECRET_KEYS:
                raise ConfigError(f"{where}.{key} is not allowed. Name an environment variable instead")
            _reject_secrets(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets(item, f"{where}[{index}]")


def _segment(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _SEGMENT.fullmatch(text):
        raise ConfigError(f"{label} must be a path segment")
    return text


def _env_name(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _ENV.fullmatch(text):
        raise ConfigError(f"{label} must be an environment variable name")
    return text


def load_config(path: Path) -> CentralConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping")
    _reject_secrets(raw, "config")
    edges_raw = raw.get("edges")
    if not isinstance(edges_raw, list) or not edges_raw:
        raise ConfigError("edges must be a non-empty list")
    edges: list[EdgeConfig] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(edges_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"edges[{index}] must be a mapping")
        edge = EdgeConfig(
            name=_segment(item.get("name"), f"edges[{index}].name"),
            site=_segment(item.get("site"), f"edges[{index}].site"),
            base_url=str(item.get("base_url") or "").strip().rstrip("/"),
            token_env=_env_name(item.get("token_env"), f"edges[{index}].token_env"),
            tls_verify=bool(item.get("tls_verify", True)),
        )
        if not edge.base_url.lower().startswith(("https://", "http://")):
            raise ConfigError(f"edges[{index}].base_url must be an http(s) URL")
        if edge.key in seen:
            raise ConfigError(f"duplicate edge {edge.site}/{edge.name}")
        seen.add(edge.key)
        edges.append(edge)
    try:
        keep = int(raw.get("keep", 8))
        port = int(raw.get("port", 8780))
        interval = float(raw.get("interval_s", 3600))
        attempts = int(raw.get("pull_attempts", 3))
        backoff = float(raw.get("pull_backoff_s", 1))
        limit = int(raw.get("max_upload_bytes", 32 * 1024 * 1024))
    except (TypeError, ValueError) as exc:
        raise ConfigError("keep, port, interval_s, pull_attempts, and max_upload_bytes must be numbers") from exc
    if keep < 1:
        raise ConfigError("keep must be at least 1")
    if not (1 <= port <= 65535):
        raise ConfigError("port must be 1..65535")
    if interval < 30:
        raise ConfigError("interval_s must be at least 30")
    if attempts < 1:
        raise ConfigError("pull_attempts must be at least 1")
    root = path.parent
    data_dir = Path(str(raw.get("data_dir") or "data"))
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    shapes = raw.get("shapes") or None
    ontology = raw.get("ontology_dir") or None
    return CentralConfig(
        edges=edges,
        data_dir=data_dir,
        keep=keep,
        interval_s=interval,
        port=port,
        interface=str(raw.get("interface") or "Ethernet").strip() or "Ethernet",
        token_env=_env_name(raw.get("token_env") or "CENTRAL_TOKEN", "token_env"),
        sparql=bool(raw.get("sparql", False)),
        max_upload_bytes=limit,
        pull_attempts=attempts,
        pull_backoff_s=max(0.0, backoff),
        shapes=(root / str(shapes)).resolve() if shapes else None,
        ontology_dir=(root / str(ontology)).resolve() if ontology else None,
        config_dir=root,
    )
