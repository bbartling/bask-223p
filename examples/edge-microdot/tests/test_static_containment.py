"""Regression tests for unauthenticated /static path traversal."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from edge_microdot.app import create_app
from edge_microdot.cache import HotCache
from edge_microdot.config import LoadedConfig
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointTarget
from edge_microdot.security import load_auth_from_env
from edge_microdot.sources.fake import FakePollSource
from edge_microdot.sparql_kb import SparqlKb

STATIC_DIR = Path(__file__).resolve().parents[1] / "edge_microdot" / "static"
SENTINEL = "static-containment-sentinel-32b-min"


def _body(resp) -> bytes:
    text = resp.text or ""
    if isinstance(text, str):
        return text.encode()
    return bytes(text)


@pytest.fixture
def edge_app(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGE_AUTH_DISABLED", "1")
    monkeypatch.setenv("EDGE_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setenv("EDGE_SECRET_KEY", SENTINEL)
    targets = [PointTarget(site="demo", device="1", object_id="analog-input,1", units="C")]
    history = HistoryStore(tmp_path / "h.duckdb")
    hot = HotCache("dict")
    app = create_app(
        source=FakePollSource(targets),
        source_kind="fake",
        targets=targets,
        cfg=LoadedConfig(site="demo"),
        hot=hot,
        history=history,
        fdd=FddEngine(history=history),
        sparql=SparqlKb(None),
        auth=load_auth_from_env(),
        site="demo",
        poll_interval_s=3600,
        flush_interval_s=3600,
        fdd_interval_s=3600,
    )
    yield app
    history.close()
    hot.close()


def _get(app, path: str):
    from microdot.test_client import TestClient

    async def _run():
        client = TestClient(app)
        return await client.get(path)

    return asyncio.run(_run())


def test_static_serves_packaged_file(edge_app):
    resp = _get(edge_app, "/static/index.html")
    assert resp.status_code == 200
    assert b"edge-microdot" in _body(resp)


@pytest.mark.parametrize(
    "path",
    [
        "/static//etc/hostname",
        "/static/../../../etc/hostname",
        "/static/../../../../etc/hostname",
        "/static/" + "../" * 12 + "etc/hostname",
        "/static/" + "../" * 12 + "proc/self/environ",
        "/static/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/hostname",
        "/static/%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fhostname",
        "/static/%252e%252e/%252e%252e/%252e%252e/etc/hostname",
        "/static/%2fetc%2fhostname",
        "/static/%2f%2fetc%2fhostname",
        "/static/sql.js/../../../../../../etc/hostname",
        "/static/index.html/../../../etc/hostname",
        "/static/./../../etc/hostname",
        "/static//proc/self/environ",
        "/static/../../../proc/self/environ",
        "/static/%2e%2e/%2e%2e/%2e%2e/%2e%2e/proc/self/environ",
        "/static/%2fproc%2fself%2fenviron",
        "/static/%252fproc%252fself%252fenviron",
    ],
)
def test_static_rejects_path_traversal(edge_app, path):
    resp = _get(edge_app, path)
    body = _body(resp)
    assert resp.status_code == 404
    assert SENTINEL.encode() not in body
    hostname = Path("/etc/hostname")
    if hostname.is_file():
        assert hostname.read_bytes().strip() not in body


def test_static_rejects_symlink_out(edge_app, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text(SENTINEL, encoding="utf-8")
    link = STATIC_DIR / "escape-link"
    link.symlink_to(outside)
    try:
        resp = _get(edge_app, "/static/escape-link")
    finally:
        link.unlink(missing_ok=True)
    body = _body(resp)
    assert resp.status_code == 404
    assert SENTINEL.encode() not in body


def test_static_allows_symlink_inside_tree(edge_app):
    link = STATIC_DIR / "index-link.html"
    link.symlink_to(STATIC_DIR / "index.html")
    try:
        resp = _get(edge_app, "/static/index-link.html")
    finally:
        link.unlink(missing_ok=True)
    assert resp.status_code == 200
    assert b"edge-microdot" in _body(resp)
