"""Opt-in push of model.ttl and export.zip to a GitHub release or repository.

Publishing is off unless ``EDGE_PUBLISH_ENABLED`` is set. The token is read
from ``EDGE_PUBLISH_TOKEN`` and is never included in logs or return values.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import re
from typing import Any, Mapping, Sequence

from edge_microdot.share import build_export_zip, env_flag, serialize_model

LOG = logging.getLogger("edge.publish")

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REF = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_API = "https://api.github.com"


class PublishDisabled(RuntimeError):
    """Raised when the opt-in flag is off."""


class PublishError(RuntimeError):
    """Raised when a publish cannot be completed. The message has no token."""


def publish_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return env_flag("EDGE_PUBLISH_ENABLED", environ)


def _settings(environ: Mapping[str, str]) -> dict[str, str]:
    if not publish_enabled(environ):
        raise PublishDisabled(
            "Publishing is off. Set EDGE_PUBLISH_ENABLED=1 to opt in. "
            "Site data may be sensitive."
        )
    token = environ.get("EDGE_PUBLISH_TOKEN", "").strip()
    if not token:
        raise PublishError("EDGE_PUBLISH_TOKEN is unset")
    repo = environ.get("EDGE_PUBLISH_REPO", "").strip()
    if not _REPO.fullmatch(repo):
        raise PublishError("EDGE_PUBLISH_REPO must be owner/name")
    kind = environ.get("EDGE_PUBLISH_KIND", "release").strip().lower() or "release"
    if kind not in {"release", "repo"}:
        raise PublishError("EDGE_PUBLISH_KIND must be release or repo")
    if kind == "release":
        ref = environ.get("EDGE_PUBLISH_TAG", "edge-share").strip() or "edge-share"
    else:
        ref = environ.get("EDGE_PUBLISH_BRANCH", "edge-share").strip() or "edge-share"
    if not _REF.fullmatch(ref):
        raise PublishError("EDGE_PUBLISH_TAG or EDGE_PUBLISH_BRANCH is not a valid ref")
    return {"token": token, "repo": repo, "kind": kind, "ref": ref}


def _call(transport, method: str, url: str, *, token: str, **kwargs):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "open-hvac-edge",
    }
    extra = kwargs.pop("headers", None) or {}
    headers.update(extra)
    return transport.request(method, url, headers=headers, timeout=60, **kwargs)


def _json_body(response) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


def _expect(response, action: str) -> dict[str, Any]:
    status = getattr(response, "status_code", 0)
    if status < 200 or status >= 300:
        raise PublishError(f"GitHub {action} failed ({status})")
    return _json_body(response)


def _publish_release(transport, *, token: str, repo: str, ref: str, model: bytes, bundle: bytes) -> None:
    tag_url = f"{_API}/repos/{repo}/releases/tags/{ref}"
    existing = _call(transport, "GET", tag_url, token=token)
    status = getattr(existing, "status_code", 0)
    if status == 404:
        created = _call(
            transport,
            "POST",
            f"{_API}/repos/{repo}/releases",
            token=token,
            json={
                "tag_name": ref,
                "name": ref,
                "body": (
                    "Opt-in edge share of model.ttl and export.zip. "
                    "Site data may be sensitive."
                ),
                "draft": False,
                "prerelease": False,
            },
        )
        release = _expect(created, "create release")
    elif 200 <= status < 300:
        release = _json_body(existing)
    else:
        raise PublishError(f"GitHub read release failed ({status})")
    upload_url = str(release.get("upload_url") or "").split("{", 1)[0]
    if not upload_url.startswith("https://"):
        raise PublishError("GitHub release did not include an upload URL")
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") in {"model.ttl", "export.zip"}:
            asset_url = str(asset.get("url") or "")
            if asset_url.startswith("https://"):
                deleted = _call(transport, "DELETE", asset_url, token=token)
                code = getattr(deleted, "status_code", 0)
                if code not in {204, 404}:
                    raise PublishError(f"GitHub delete asset failed ({code})")
    files = (
        ("model.ttl", model, "text/turtle"),
        ("export.zip", bundle, "application/zip"),
    )
    for name, payload, content_type in files:
        uploaded = _call(
            transport,
            "POST",
            upload_url,
            token=token,
            params={"name": name},
            data=payload,
            headers={"Content-Type": content_type},
        )
        _expect(uploaded, f"upload {name}")


def _publish_repo(transport, *, token: str, repo: str, ref: str, model: bytes, bundle: bytes) -> None:
    files = (
        ("share/model.ttl", model, "update model.ttl"),
        ("share/export.zip", bundle, "update export.zip"),
    )
    for path, payload, message in files:
        url = f"{_API}/repos/{repo}/contents/{path}"
        current = _call(transport, "GET", url, token=token, params={"ref": ref})
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(payload).decode("ascii"),
            "branch": ref,
        }
        status = getattr(current, "status_code", 0)
        if status == 200:
            sha = _json_body(current).get("sha")
            if isinstance(sha, str) and sha:
                body["sha"] = sha
        elif status != 404:
            raise PublishError(f"GitHub read {path} failed ({status})")
        saved = _call(transport, "PUT", url, token=token, json=body)
        _expect(saved, f"write {path}")


def publish_once(
    *,
    cfg,
    targets: Sequence[Any],
    store: Any = None,
    graph=None,
    session: Any = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Push the current model and zip. Returns repo, kind, and ref. No token."""
    env = os.environ if environ is None else environ
    settings = _settings(env)
    model_graph = graph if graph is not None else getattr(cfg, "graph", None)
    model = serialize_model(model_graph, store, "turtle")
    bundle = build_export_zip(
        graph=model_graph,
        store=store,
        cfg=cfg,
        targets=targets,
        environ=env,
    )
    if session is None:
        import requests

        session = requests.Session()
    token = settings["token"]
    if settings["kind"] == "release":
        _publish_release(
            session, token=token, repo=settings["repo"], ref=settings["ref"], model=model, bundle=bundle
        )
    else:
        _publish_repo(
            session, token=token, repo=settings["repo"], ref=settings["ref"], model=model, bundle=bundle
        )
    LOG.info("published model.ttl and export.zip to %s %s %s", settings["repo"], settings["kind"], settings["ref"])
    return {"repo": settings["repo"], "kind": settings["kind"], "ref": settings["ref"], "files": ["model.ttl", "export.zip"]}


def publish_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="edge-microdot publish",
        description="Push model.ttl and export.zip. Off unless EDGE_PUBLISH_ENABLED=1.",
    )
    parser.add_argument("--points", default=None, help="223P TTL, CSV, or YAML config")
    parser.add_argument("--site", default=None)
    args = parser.parse_args(list(argv or []))
    try:
        from pathlib import Path

        from edge_microdot.cli import resolve_points_path
        from edge_microdot.config import LoadedConfig, load_config

        site = args.site or os.environ.get("EDGE_SITE", "demo-site")
        points = resolve_points_path(Path(args.points) if args.points else None)
        cfg = load_config(points, site=site) if points else LoadedConfig(site=site)
        store = None
        if env_flag("EDGE_ENABLE_SPARQL") and cfg.graph is not None and len(cfg.graph) > 0:
            from edge_microdot.sparql_kb import SparqlKb

            kb = SparqlKb(cfg.graph)
            store = kb.store if kb.enabled else None
        result = publish_once(cfg=cfg, targets=cfg.targets, store=store, graph=cfg.graph)
    except PublishDisabled as exc:
        print(str(exc))
        return 2
    except PublishError as exc:
        print(str(exc))
        return 1
    print(f"published {result['kind']} {result['repo']} {result['ref']}")
    return 0


async def scheduled_publish(cfg, targets: Sequence[Any], store: Any) -> None:
    """Publish shortly after startup, then on ``EDGE_PUBLISH_INTERVAL``."""
    try:
        interval = float(os.environ.get("EDGE_PUBLISH_INTERVAL", "86400"))
    except ValueError:
        interval = 86400.0
    interval = max(60.0, interval)
    await asyncio.sleep(min(5.0, interval))
    while True:
        try:
            await asyncio.to_thread(publish_once, cfg=cfg, targets=list(targets), store=store)
        except PublishDisabled:
            return
        except Exception as exc:
            LOG.warning("scheduled publish failed: %s", exc)
        await asyncio.sleep(interval)
