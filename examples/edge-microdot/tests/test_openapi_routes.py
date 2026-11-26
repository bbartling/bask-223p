"""The OpenAPI document lists every Microdot route, and the prose does too."""
from __future__ import annotations

import re
from pathlib import Path

from edge_microdot.app import create_app
from edge_microdot.cache import HotCache
from edge_microdot.config import LoadedConfig
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointTarget
from edge_microdot.openapi_spec import build_openapi
from edge_microdot.security import load_auth_from_env
from edge_microdot.sources.fake import FakePollSource
from edge_microdot.sparql_kb import SparqlKb

_SEGMENT = re.compile(r"<(?:[^:>]+:)?([^>]+)>")
_DOCS = Path(__file__).resolve().parents[3] / "docs" / "api.md"


def _app_routes(app) -> set[tuple[str, str]]:
    found = set()
    for methods, pattern, _handler, prefix, _sub in app.url_map:
        path = _SEGMENT.sub(r"{\1}", prefix + pattern.url_pattern)
        for method in methods:
            found.add((method.upper(), path))
    return found


def _spec_routes(spec: dict) -> set[tuple[str, str]]:
    found = set()
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method.startswith("x-") or not isinstance(operation, dict):
                continue
            found.add((method.upper(), path))
    return found


def test_openapi_lists_every_route(tmp_path):
    import asyncio
    import base64

    from microdot.test_client import TestClient

    history = HistoryStore(tmp_path / "h.duckdb")
    app = create_app(
        source=FakePollSource([]),
        source_kind="fake",
        targets=[PointTarget(site="demo", device="1", object_id="analog-input,1")],
        cfg=LoadedConfig(site="demo"),
        hot=HotCache("dict"),
        history=history,
        fdd=FddEngine(history=history),
        sparql=SparqlKb(None),
        auth=load_auth_from_env(),
        site="demo",
        poll_interval_s=3600,
        flush_interval_s=3600,
        fdd_interval_s=3600,
    )
    spec = build_openapi()
    assert _app_routes(app) == _spec_routes(spec)
    text = _DOCS.read_text(encoding="utf-8")
    for _method, path in _spec_routes(spec):
        assert path in text
    assert "EDGE_RULES_WRITE" in text
    assert "EDGE_PUBLIC_MODEL" in text
    assert "EDGE_PUBLISH_ENABLED" in text

    async def _go():
        client = TestClient(app)
        hidden = await client.get("/api/openapi.json")
        assert hidden.status_code == 401
        token = await client.post(
            "/auth/token",
            headers={"Authorization": "Basic " + base64.b64encode(b"operator:operator-pass").decode("ascii")},
        )
        body = await client.get(
            "/api/openapi.json",
            headers={"Authorization": f"Bearer {token.json['access_token']}"},
        )
        assert body.status_code == 200
        assert body.headers["Content-Type"].startswith("application/json")
        assert set(body.json["paths"]) == {path for _method, path in _spec_routes(spec)}

    asyncio.run(_go())
    history.close()
