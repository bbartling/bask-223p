"""Read-only model, point, and config export for the edge.

Secrets are removed by copying allowlisted keys only. Unknown keys are
dropped. There is no denylist.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import jwt
import yaml
from microdot import Response
from rdflib import Graph

from edge_microdot import __version__
from edge_microdot.models import PointTarget
from edge_microdot.role_terms import input_term_from_target
from edge_microdot.security import EdgeUser, client_addr, extract_bearer_token

LOG = logging.getLogger("edge.share")

ROLE_FILE_NAME = "roles/openfdd-roles.json"
CSV_COLUMNS = (
    "site",
    "label",
    "role",
    "units",
    "source",
    "device",
    "object_id",
    "property",
    "ref",
    "address",
)

# Nested values are allow maps. True means a scalar (or a list of scalars).
CONFIG_ALLOW: dict[str, Any] = {
    "site": True,
    "driver": True,
    "source": True,
    "base_ord": True,
    "local_address": True,
    "max_props": True,
    "interval_s": True,
    "poll_interval_s": True,
    "flush_interval_s": True,
    "fdd_interval_s": True,
    "snapshot_interval_s": True,
    "sparql_enabled": True,
    "hot_backend": True,
    "point_count": True,
    "public_model": True,
    "publish_enabled": True,
    "auth_required": True,
    "points": {
        "site": True,
        "label": True,
        "role": True,
        "units": True,
        "source": True,
        "device": True,
        "object_id": True,
        "property_id": True,
        "ref": True,
        "ord_path": True,
        "device_address": True,
    },
}

_RDF_MEDIA = {
    "text/turtle": "turtle",
    "application/x-turtle": "turtle",
    "application/ld+json": "json-ld",
    "application/n-triples": "nt",
}
_RDF_RDFLIB = {"turtle": "turtle", "json-ld": "json-ld", "nt": "nt"}
_RDF_CONTENT = {
    "turtle": "text/turtle; charset=utf-8",
    "json-ld": "application/ld+json; charset=utf-8",
    "nt": "application/n-triples; charset=utf-8",
}
_RDF_FILENAME = {"turtle": "model.ttl", "json-ld": "model.jsonld", "nt": "model.nt"}

_EXPORT_HITS: dict[str, list[float]] = {}


def reset_export_limits() -> None:
    _EXPORT_HITS.clear()


def env_flag(name: str, environ: Mapping[str, str] | None = None, *, default: bool = False) -> bool:
    source = os.environ if environ is None else environ
    if name not in source:
        return default
    raw = source.get(name) or ""
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def public_model_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return env_flag("EDGE_PUBLIC_MODEL", environ)


def export_limited(key: str) -> bool:
    """True when this client has used its export budget for the window."""
    try:
        limit = int(os.environ.get("EDGE_EXPORT_RATE_LIMIT", "12"))
        window = float(os.environ.get("EDGE_EXPORT_RATE_WINDOW", "60"))
    except ValueError:
        limit, window = 12, 60.0
    if limit <= 0 or window <= 0:
        return False
    now = time.monotonic()
    recent = [stamp for stamp in _EXPORT_HITS.get(key, []) if now - stamp < window]
    if len(recent) >= limit:
        _EXPORT_HITS[key] = recent
        return True
    recent.append(now)
    _EXPORT_HITS[key] = recent
    return False


def role_file_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """sha256 of the vendored Open-FDD roles file, when that file is present."""
    env = environ if environ is not None else os.environ
    root = Path(__file__).resolve().parents[1]
    candidates: list[Path] = []
    override = env.get("EDGE_ROLES_FILE", "").strip()
    if override:
        candidates.append(Path(override))
    candidates.append(root / "roles" / "openfdd-roles.json")
    candidates.append(root / "vendor" / "open-fdd" / "roles" / "openfdd-roles.json")
    for path in candidates:
        try:
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                return {"name": ROLE_FILE_NAME, "present": True, "sha256": digest}
        except OSError:
            continue
    return {"name": ROLE_FILE_NAME, "present": False, "sha256": None}


def point_source_kind(target: PointTarget) -> str:
    """``bacnet`` or ``modbus`` for a configured point, else an empty string."""
    extra = target.extra if isinstance(target.extra, dict) else {}
    protocol = str(extra.get("protocol") or "").strip().lower()
    if protocol in {"bacnet", "modbus"}:
        return protocol
    ref = str(target.ref or "").lower()
    if ref.startswith("modbus:"):
        return "modbus"
    if ref.startswith("bacnet:") or (target.device and "," in (target.object_id or "")):
        return "bacnet"
    return ""


def configured_point_row(target: PointTarget) -> dict[str, str]:
    return {
        "site": target.site or "",
        "label": target.label or "",
        "role": input_term_from_target(target) or "",
        "units": target.units or "",
        "source": point_source_kind(target),
        "device": target.device or "",
        "object_id": target.object_id or "",
        "property": target.property_id or "",
        "property_id": target.property_id or "",
        "ref": target.ref or "",
        "address": target.device_address or "",
        "device_address": target.device_address or "",
        "ord_path": target.ord_path or "",
    }


def points_csv_bytes(targets: Sequence[PointTarget]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for target in targets:
        writer.writerow(configured_point_row(target))
    return buf.getvalue().encode("utf-8")


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list) and all(item is None or isinstance(item, (str, int, float, bool)) for item in value):
        return list(value)
    return None


def redact_config(value: Any, allow: Mapping[str, Any] | bool = CONFIG_ALLOW) -> Any:
    """Copy ``value`` keeping only allowlisted keys.

    A key that is not listed is omitted, including keys whose names do not
    look secret. Nested objects and lists of objects use the nested allow map.
    """
    if allow is True:
        return _scalar(value)
    if not isinstance(allow, Mapping):
        return None
    if isinstance(value, list):
        out_list = []
        for item in value:
            if isinstance(item, dict):
                cleaned = redact_config(item, allow)
                if isinstance(cleaned, dict):
                    out_list.append(cleaned)
        return out_list
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key, rule in allow.items():
        if key not in value:
            continue
        cleaned = redact_config(value[key], rule)
        if cleaned is None:
            continue
        out[key] = cleaned
    return out


def _optional_number(env: Mapping[str, str], name: str) -> float | None:
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def export_config_document(
    cfg: Any,
    targets: Sequence[PointTarget],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Allowlisted runtime settings and configured points.

    Environment variables are read by name from a fixed set. The process
    environment is never copied wholesale, and point ``extra`` is never copied.
    """
    env = environ if environ is not None else os.environ
    local = str(getattr(cfg, "local_address", "") or "")
    if local in {"", "0.0.0.0", "0.0.0.0/0"}:
        local = ""
    raw = {
        "site": getattr(cfg, "site", "") or "",
        "driver": (env.get("EDGE_DRIVER") or getattr(cfg, "source", "") or "").strip(),
        "source": getattr(cfg, "source", None),
        "base_ord": getattr(cfg, "base_ord", "") or "",
        "local_address": local or None,
        "max_props": getattr(cfg, "max_props", None),
        "interval_s": getattr(cfg, "interval_s", None),
        "poll_interval_s": _optional_number(env, "EDGE_POLL_INTERVAL"),
        "flush_interval_s": _optional_number(env, "EDGE_FLUSH_INTERVAL"),
        "fdd_interval_s": _optional_number(env, "EDGE_FDD_INTERVAL"),
        "snapshot_interval_s": _optional_number(env, "EDGE_SNAPSHOT_INTERVAL"),
        "sparql_enabled": env_flag("EDGE_ENABLE_SPARQL", env, default=True),
        "hot_backend": (env.get("EDGE_HOT_BACKEND") or "").strip() or None,
        "point_count": len(list(targets)),
        "public_model": public_model_enabled(env),
        "publish_enabled": env_flag("EDGE_PUBLISH_ENABLED", env),
        "auth_required": not env_flag("EDGE_AUTH_DISABLED", env),
        "points": [configured_point_row(target) for target in targets],
    }
    redacted = redact_config(raw)
    return redacted if isinstance(redacted, dict) else {}


def choose_rdf_format(accept: str | None) -> str | None:
    """Pick turtle, JSON-LD, or N-Triples from an Accept header."""
    if accept is None or not str(accept).strip():
        return "turtle"
    best: str | None = None
    best_q = -1.0
    for part in str(accept).split(","):
        bits = [bit.strip() for bit in part.split(";") if bit.strip()]
        if not bits:
            continue
        media = bits[0].lower()
        quality = 1.0
        for bit in bits[1:]:
            if bit.lower().startswith("q="):
                try:
                    quality = float(bit.split("=", 1)[1])
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        fmt = _RDF_MEDIA.get(media)
        if media == "*/*":
            fmt = "turtle"
        if fmt is None:
            continue
        if quality > best_q:
            best = fmt
            best_q = quality
    return best


def serialize_model(graph: Graph | None, store: Any, fmt: str) -> bytes:
    """Serialize the 223P graph. Oxigraph is used when a store is already loaded."""
    if fmt not in _RDF_RDFLIB:
        raise ValueError(f"unsupported RDF format: {fmt}")
    model = graph if graph is not None else Graph()
    if store is not None:
        try:
            import pyoxigraph

            ox_fmt = {
                "turtle": pyoxigraph.RdfFormat.TURTLE,
                "json-ld": pyoxigraph.RdfFormat.JSON_LD,
                "nt": pyoxigraph.RdfFormat.N_TRIPLES,
            }[fmt]
            data = store.dump(format=ox_fmt, from_graph=pyoxigraph.DefaultGraph())
            if isinstance(data, str):
                data = data.encode("utf-8")
            if isinstance(data, (bytes, bytearray)) and (data.strip() or len(model) == 0):
                return bytes(data)
        except Exception:
            LOG.debug("oxigraph serialization failed; using rdflib", exc_info=True)
    text = model.serialize(format=_RDF_RDFLIB[fmt])
    if isinstance(text, bytes):
        return text
    return str(text).encode("utf-8")


def choose_config_format(request) -> str | None:
    query = str(request.args.get("format") or "").strip().lower()
    if query in {"json"}:
        return "json"
    if query in {"yaml", "yml"}:
        return "yaml"
    if query:
        return None
    accept = str(request.headers.get("Accept") or "")
    if not accept.strip() or "*/*" in accept:
        return "json"
    best: str | None = None
    best_q = -1.0
    for part in accept.split(","):
        bits = [bit.strip() for bit in part.split(";") if bit.strip()]
        if not bits:
            continue
        media = bits[0].lower()
        quality = 1.0
        for bit in bits[1:]:
            if bit.lower().startswith("q="):
                try:
                    quality = float(bit.split("=", 1)[1])
                except ValueError:
                    quality = 0.0
        fmt = None
        if media in {"application/yaml", "text/yaml", "application/x-yaml"}:
            fmt = "yaml"
        elif media == "application/json":
            fmt = "json"
        elif media == "*/*":
            fmt = "json"
        if fmt is None or quality <= 0:
            continue
        if quality > best_q:
            best = fmt
            best_q = quality
    return best


def config_bytes(document: Mapping[str, Any], fmt: str) -> tuple[bytes, str, str]:
    if fmt == "yaml":
        text = yaml.safe_dump(dict(document), sort_keys=True, allow_unicode=True)
        return text.encode("utf-8"), "application/yaml; charset=utf-8", "config.yaml"
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    return text.encode("utf-8"), "application/json; charset=utf-8", "config.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(
    files: Mapping[str, bytes],
    *,
    generated_at: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    stamp = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "generated_at": stamp,
        "edge_version": __version__,
        "role_file": role_file_status(environ),
        "files": {
            name: {"sha256": _sha256(payload), "bytes": len(payload)}
            for name, payload in files.items()
        },
    }


def build_export_zip(
    *,
    graph: Graph | None,
    store: Any,
    cfg: Any,
    targets: Sequence[PointTarget],
    environ: Mapping[str, str] | None = None,
    generated_at: str | None = None,
) -> bytes:
    model = serialize_model(graph, store, "turtle")
    table = points_csv_bytes(targets)
    document = export_config_document(cfg, targets, environ=environ)
    config, _content_type, _filename = config_bytes(document, "json")
    files = {"model.ttl": model, "points.csv": table, "config.json": config}
    manifest = build_manifest(files, generated_at=generated_at, environ=environ)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
        archive.writestr("manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return buf.getvalue()


def etag_for(payload: bytes) -> str:
    return f'"{_sha256(payload)}"'


def http_date(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return format_datetime(when.astimezone(timezone.utc), usegmt=True)


def is_not_modified(request, etag: str, modified: datetime) -> bool:
    quoted = etag if etag.startswith('"') else f'"{etag}"'
    inm = request.headers.get("If-None-Match")
    if inm:
        tags = []
        for part in str(inm).split(","):
            token = part.strip()
            if token.upper().startswith("W/"):
                token = token[2:].strip()
            tags.append(token)
        return "*" in tags or quoted in tags
    ims = request.headers.get("If-Modified-Since")
    if not ims:
        return False
    try:
        when = parsedate_to_datetime(str(ims))
    except (TypeError, ValueError, IndexError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=timezone.utc)
    return int(when.timestamp()) >= int(modified.timestamp())


class _TempStream:
    """On-disk body streamed the same way Microdot streams ``send_file``."""

    def __init__(self, payload: bytes) -> None:
        handle = tempfile.NamedTemporaryFile(prefix="edge-share-", delete=False)
        try:
            handle.write(payload)
            handle.flush()
            self._path = Path(handle.name)
        finally:
            handle.close()
        self._read = self._path.open("rb")

    def read(self, n: int = -1) -> bytes:
        return self._read.read(n)

    def close(self) -> None:
        try:
            self._read.close()
        finally:
            self._path.unlink(missing_ok=True)


def streaming_response(
    payload: bytes,
    *,
    content_type: str,
    filename: str,
    modified: datetime,
    disposition: str = "attachment",
    extra_headers: Mapping[str, str] | None = None,
) -> Response:
    response = Response.send_file(
        filename,
        content_type=content_type.split(";", 1)[0],
        stream=_TempStream(payload),
        max_age=0,
    )
    response.headers["Content-Type"] = content_type
    response.headers["ETag"] = etag_for(payload)
    response.headers["Last-Modified"] = http_date(modified)
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    if extra_headers:
        for key, value in extra_headers.items():
            response.headers[key] = value
    return response


def api_error(code: str, message: str, status: int, **headers: str) -> Response:
    response = Response({"error": {"code": code, "message": message}}, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    for key, value in headers.items():
        response.headers[key] = value
    return response


def authorize_read(auth, request) -> EdgeUser | Response:
    """Bearer access token, or an Open-FDD UI access token of any read role."""
    if auth.disabled:
        return EdgeUser("anonymous")
    token = extract_bearer_token(request)
    if not token:
        return api_error("unauthorized", "Authentication required", 401, **{"WWW-Authenticate": "Bearer"})
    user = auth.tokens.verify_access(token)
    if user is not None:
        return user
    try:
        payload = jwt.decode(token, auth.tokens.secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return api_error("unauthorized", "Authentication required", 401, **{"WWW-Authenticate": "Bearer"})
    if payload.get("typ") != "ui-access":
        return api_error("unauthorized", "Authentication required", 401, **{"WWW-Authenticate": "Bearer"})
    role = str(payload.get("role") or "")
    if role not in {"viewer", "operator", "admin"}:
        return api_error("forbidden", "viewer role is required", 403)
    return EdgeUser(str(payload.get("sub") or ""))


def _conditional(request, payload: bytes, modified: datetime, **kwargs) -> Response:
    tag = etag_for(payload)
    if is_not_modified(request, tag, modified):
        return Response(
            b"",
            status_code=304,
            headers={
                "ETag": tag,
                "Last-Modified": http_date(modified),
                "Cache-Control": "private, no-cache",
            },
        )
    return streaming_response(payload, modified=modified, **kwargs)


def attach_share_api(
    app,
    *,
    auth,
    cfg,
    targets: Sequence[PointTarget],
    graph: Graph | None,
    store: Any,
    started_at: datetime | None = None,
) -> None:
    """Register the read-only share routes. ``targets`` is snapshotted now."""
    configured = list(targets)
    modified = started_at or datetime.now(timezone.utc).replace(microsecond=0)
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=timezone.utc)

    def _limited(request) -> Response | None:
        if export_limited(client_addr(request)):
            window = os.environ.get("EDGE_EXPORT_RATE_WINDOW", "60")
            return api_error("rate_limited", "Too many requests", 429, **{"Retry-After": str(window)})
        return None

    def _guard(request, *, public: bool) -> Response | None:
        if public and public_model_enabled():
            return _limited(request)
        user = authorize_read(auth, request)
        if isinstance(user, Response):
            return user
        return _limited(request)

    @app.get("/api/model.ttl")
    async def model_ttl(request):
        denied = _guard(request, public=True)
        if denied is not None:
            return denied
        payload = serialize_model(graph, store, "turtle")
        return _conditional(
            request,
            payload,
            modified,
            content_type=_RDF_CONTENT["turtle"],
            filename="model.ttl",
            disposition="inline",
            extra_headers={"Vary": "Authorization"},
        )

    @app.get("/api/model")
    async def model_negotiated(request):
        denied = _guard(request, public=False)
        if denied is not None:
            return denied
        fmt = choose_rdf_format(request.headers.get("Accept"))
        if fmt is None:
            return api_error(
                "not_acceptable",
                "Accept must be text/turtle, application/ld+json, or application/n-triples",
                406,
            )
        payload = serialize_model(graph, store, fmt)
        return _conditional(
            request,
            payload,
            modified,
            content_type=_RDF_CONTENT[fmt],
            filename=_RDF_FILENAME[fmt],
            disposition="inline",
            extra_headers={"Vary": "Accept, Authorization"},
        )

    @app.get("/api/points.csv")
    async def points_csv(request):
        denied = _guard(request, public=False)
        if denied is not None:
            return denied
        payload = points_csv_bytes(configured)
        return _conditional(
            request,
            payload,
            modified,
            content_type="text/csv; charset=utf-8",
            filename="points.csv",
        )

    @app.get("/api/config/export")
    async def config_export(request):
        denied = _guard(request, public=False)
        if denied is not None:
            return denied
        fmt = choose_config_format(request)
        if fmt is None:
            return api_error("not_acceptable", "format must be json or yaml", 406)
        document = export_config_document(cfg, configured)
        payload, content_type, filename = config_bytes(document, fmt)
        return _conditional(
            request,
            payload,
            modified,
            content_type=content_type,
            filename=filename,
            extra_headers={"Vary": "Accept, Authorization"},
        )

    @app.get("/api/export.zip")
    async def export_zip(request):
        denied = _guard(request, public=False)
        if denied is not None:
            return denied
        payload = build_export_zip(graph=graph, store=store, cfg=cfg, targets=configured)
        return _conditional(
            request,
            payload,
            datetime.now(timezone.utc),
            content_type="application/zip",
            filename="export.zip",
        )

    @app.get("/api/openapi.json")
    async def openapi_json(request):
        user = authorize_read(auth, request)
        if isinstance(user, Response):
            return user
        from edge_microdot.openapi_spec import build_openapi

        payload = (json.dumps(build_openapi(), indent=2, sort_keys=True) + "\n").encode("utf-8")
        return _conditional(
            request,
            payload,
            modified,
            content_type="application/json; charset=utf-8",
            filename="openapi.json",
            disposition="inline",
        )
