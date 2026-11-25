from __future__ import annotations

import base64
import hashlib
import os

import pytest


def _pbkdf2_hash(password: str, *, salt: str = "testsalt01", iterations: int = 310_000) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    b64 = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"pbkdf2_sha256${iterations}${salt}${b64}"


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("EDGE_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("EDGE_USERS", f"operator:{_pbkdf2_hash('operator-pass')}")
    monkeypatch.delenv("EDGE_AUTH_DISABLED", raising=False)


@pytest.fixture
def auth_headers(monkeypatch):
    monkeypatch.delenv("EDGE_AUTH_DISABLED", raising=False)

    def _issue(client) -> dict[str, str]:
        import asyncio

        async def _go():
            resp = await client.post(
                "/auth/token",
                headers={
                    "Authorization": "Basic "
                    + base64.b64encode(b"operator:operator-pass").decode("ascii")
                },
            )
            body = resp.json
            return {"Authorization": f"Bearer {body['access_token']}"}

        return asyncio.run(_go())

    return _issue