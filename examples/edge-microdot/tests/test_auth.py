from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path

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
    app.auth = auth
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
    secret = "exp-secret-at-least-32-bytes-long"
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


def test_login_cookie_is_secure_and_samesite(auth_app):
    from microdot.test_client import TestClient

    async def _run():
        client = TestClient(auth_app)
        login = await client.post(
            "/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body="username=operator&password=operator-pass",
        )
        cookie = login.headers.get("Set-Cookie") or ""
        if isinstance(cookie, list):
            cookie = "; ".join(cookie)
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Strict" in cookie

    asyncio.run(_run())


def test_live_rejects_foreign_origin(auth_app):
    from microdot.test_client import TestClient

    async def _run():
        client = TestClient(auth_app)
        await client.post(
            "/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body="username=operator&password=operator-pass",
        )
        denied = await client.get("/live", headers={"Origin": "https://other.example"})
        assert denied.status_code == 403
        allowed = await client.get("/live", headers={"Origin": "http://example.com:1234"})
        assert allowed.status_code != 403

    asyncio.run(_run())


def test_ws_token_is_access_only(auth_app):
    from microdot.test_client import TestClient

    async def _run():
        client = TestClient(auth_app)
        await client.post(
            "/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body="username=operator&password=operator-pass",
        )
        resp = await client.get("/auth/ws-token")
        assert resp.status_code == 200
        assert "access_token" in resp.json
        assert "refresh_token" not in resp.json
        assert resp.json["expires_in"] <= 300

    asyncio.run(_run())


def test_dashboard_fault_cells_use_text_content():
    html = (
        Path(__file__).resolve().parents[1] / "edge_microdot" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert "tr.innerHTML" not in html
    assert "td.textContent" in html


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
    monkeypatch.setenv("EDGE_ALLOW_UNAUTHENTICATED", "1")
    with pytest.raises(RuntimeError, match="EDGE_SECRET_KEY"):
        load_auth_from_env()


def test_startup_rejects_short_secret(monkeypatch):
    monkeypatch.setenv("EDGE_SECRET_KEY", "too-short")
    with pytest.raises(RuntimeError, match="32 bytes"):
        load_auth_from_env()


def test_auth_disabled_requires_explicit_allow(monkeypatch):
    monkeypatch.setenv("EDGE_AUTH_DISABLED", "1")
    monkeypatch.delenv("EDGE_ALLOW_UNAUTHENTICATED", raising=False)
    with pytest.raises(RuntimeError, match="EDGE_ALLOW_UNAUTHENTICATED"):
        load_auth_from_env()


def test_refresh_is_single_use_and_checks_the_user(auth_app):
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
        again = await client.post(
            "/auth/refresh",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"refresh_token": refresh}),
        )
        assert again.status_code == 401
        auth_app.auth.users._by_name.clear()
        fresh = renewed.json["refresh_token"]
        gone = await client.post(
            "/auth/refresh",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"refresh_token": fresh}),
        )
        assert gone.status_code == 401

    asyncio.run(_run())


def test_login_rate_limit_returns_429(auth_app):
    from microdot.test_client import TestClient

    async def _run():
        client = TestClient(auth_app)
        last = None
        for _ in range(9):
            last = await client.post(
                "/login",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body="username=stranger&password=wrong-pass",
            )
        assert last is not None
        assert last.status_code == 429

    asyncio.run(_run())


def test_session_cookie_has_exp(auth_app):
    from edge_microdot.security import _subkey
    from microdot.test_client import TestClient

    async def _run():
        client = TestClient(auth_app)
        login = await client.post(
            "/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body="username=operator&password=operator-pass",
        )
        cookie = login.headers.get("Set-Cookie") or ""
        if isinstance(cookie, list):
            cookie = "; ".join(cookie)
        token = ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("session="):
                token = part.split("=", 1)[1]
        secret = _subkey("unit-test-secret-key-32-bytes-minimum", "edge-session")
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        assert payload["exp"] > int(time.time())

    asyncio.run(_run())