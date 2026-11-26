from __future__ import annotations

import asyncio
import base64
import time

import jwt
import pytest

from edge_microdot.app import create_app
from edge_microdot.cache import HotCache
from edge_microdot.config import LoadedConfig
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointTarget
from edge_microdot.security import EdgeUser, TokenService, load_auth_from_env
from edge_microdot.sources.fake import FakePollSource
from edge_microdot.sparql_kb import SparqlKb


def _basic_header(user: str, password: str) -> dict[str, str]:
    tok = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {tok}"}


@pytest.fixture
def auth_app(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGE_AUTH_DISABLED", raising=False)
    targets = [PointTarget(site="demo", device="1", object_id="analog-input,1", units="C")]
    auth = load_auth_from_env()
    history = HistoryStore(tmp_path / "a.duckdb")
    app = create_app(
        source=FakePollSource(targets),
        source_kind="fake",
        targets=targets,
        cfg=LoadedConfig(site="demo"),
        hot=HotCache("dict"),
        history=history,
        fdd=FddEngine(history=history),
        sparql=SparqlKb(None),
        auth=auth,
        site="demo",
        poll_interval_s=3600,
        flush_interval_s=3600,
        fdd_interval_s=3600,
    )
    return app


def test_issue_and_verify_token(auth_app):
    from microdot.test_client import TestClient

    async def _run():
        client = TestClient(auth_app)
        resp = await client.post("/auth/token", headers=_basic_header("operator", "operator-pass"))
        assert resp.status_code == 200
        body = resp.json
        assert body["token_type"] == "Bearer"
        latest = await client.get("/latest", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert latest.status_code == 200

    asyncio.run(_run())


def test_invalid_token_rejected(auth_app):
    from microdot.test_client import TestClient

    async def _run():
        client = TestClient(auth_app)
        resp = await client.get("/latest", headers={"Authorization": "Bearer not-a-real-token"})
        assert resp.status_code == 401

    asyncio.run(_run())


def test_expired_token_rejected(monkeypatch):
    secret = "exp-secret"
    svc = TokenService(secret, access_ttl=1)
    now = int(time.time())
    expired = jwt.encode(
        {"sub": "operator", "typ": "access", "iat": now - 120, "exp": now - 60},
        secret,
        algorithm="HS256",
    )
    user = svc.verify_access(expired)
    assert user is None


def test_refresh_rotation(auth_app):
    from microdot.test_client import TestClient

    async def _run():
        client = TestClient(auth_app)
        first = await client.post("/auth/token", headers=_basic_header("operator", "operator-pass"))
        refresh = first.json["refresh_token"]
        import json

        renewed = await client.post(
            "/auth/refresh",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"refresh_token": refresh}),
        )
        assert renewed.status_code == 200
        assert renewed.json["access_token"] != first.json["access_token"]

    asyncio.run(_run())


def test_live_rejects_query_token(auth_app):
    from microdot.test_client import TestClient

    async def _run():
        client = TestClient(auth_app)
        denied = await client.get("/live?token=not-a-header")
        assert denied.status_code == 401

    asyncio.run(_run())


def test_login_logout_flow(auth_app):
    from microdot.test_client import TestClient

    async def _run():
        client = TestClient(auth_app)
        denied = await client.get("/")
        assert denied.status_code == 302
        login = await client.post(
            "/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body="username=operator&password=operator-pass",
        )
        assert login.status_code == 302
        home = await client.get("/")
        assert home.status_code == 200
        out = await client.get("/logout")
        assert out.status_code == 302

    asyncio.run(_run())


def test_startup_fails_when_secret_key_unset(monkeypatch):
    monkeypatch.delenv("EDGE_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="EDGE_SECRET_KEY"):
        load_auth_from_env()


def test_startup_fails_when_secret_key_blank(monkeypatch):
    monkeypatch.setenv("EDGE_SECRET_KEY", "   ")
    with pytest.raises(RuntimeError, match="EDGE_SECRET_KEY"):
        load_auth_from_env()


def test_startup_fails_when_secret_unset_even_if_auth_disabled(monkeypatch):
    monkeypatch.delenv("EDGE_SECRET_KEY", raising=False)
    monkeypatch.setenv("EDGE_AUTH_DISABLED", "1")
    with pytest.raises(RuntimeError, match="EDGE_SECRET_KEY"):
        load_auth_from_env()