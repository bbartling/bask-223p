"""Microdot TokenAuth, BasicAuth, and session login (secrets from env only)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from microdot import Response, redirect
from microdot.auth import BasicAuth, TokenAuth
from microdot.login import Login
from microdot.session import Session, with_session


@dataclass(slots=True)
class EdgeUser:
    username: str

    @property
    def id(self) -> str:
        return self.username


class UserDirectory:
    """Parse EDGE_USERS=user:pbkdf2_sha256$iter$salt$hash entries."""

    def __init__(self, raw: str | None) -> None:
        self._by_name: dict[str, str] = {}
        if not raw:
            return
        for chunk in raw.replace("\n", ";").split(";"):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            name, digest = chunk.split(":", 1)
            name = name.strip()
            if name:
                self._by_name[name] = digest.strip()

    def get(self, username: str) -> EdgeUser | None:
        return EdgeUser(username) if username in self._by_name else None

    def verify_password(self, username: str, password: str) -> bool:
        digest = self._by_name.get(username)
        if not digest:
            return False
        if digest.startswith("pbkdf2_sha256$"):
            try:
                _tag, iter_s, salt, expected = digest.split("$", 3)
                iterations = int(iter_s)
                got = hashlib.pbkdf2_hmac(
                    "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
                )
                exp = base64.urlsafe_b64decode(expected + "=" * (-len(expected) % 4))
                return hmac.compare_digest(got, exp)
            except (ValueError, TypeError):
                return False
        return False


class TokenService:
    def __init__(self, secret: bytes | str, *, access_ttl: int = 3600, refresh_ttl: int | None = None) -> None:
        self.secret = secret
        self.access_ttl = max(60, int(access_ttl))
        self.refresh_ttl = refresh_ttl if refresh_ttl is not None else self.access_ttl * 12

    def issue_pair(self, user: EdgeUser) -> dict[str, Any]:
        now = int(time.time())
        access = jwt.encode(
            {
                "sub": user.username,
                "typ": "access",
                "iat": now,
                "exp": now + self.access_ttl,
                "jti": uuid.uuid4().hex,
            },
            self.secret,
            algorithm="HS256",
        )
        refresh = jwt.encode(
            {
                "sub": user.username,
                "typ": "refresh",
                "iat": now,
                "exp": now + self.refresh_ttl,
                "jti": uuid.uuid4().hex,
            },
            self.secret,
            algorithm="HS256",
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": self.access_ttl,
        }

    def refresh(self, refresh_token: str) -> dict[str, Any] | None:
        payload = self._decode(refresh_token, expected_typ="refresh")
        if not payload:
            return None
        return self.issue_pair(EdgeUser(str(payload["sub"])))

    def verify_access(self, token: str) -> EdgeUser | None:
        payload = self._decode(token, expected_typ="access")
        if not payload:
            return None
        return EdgeUser(str(payload["sub"]))

    def _decode(self, token: str, *, expected_typ: str) -> dict[str, Any] | None:
        try:
            payload = jwt.decode(token, self.secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None
        if payload.get("typ") != expected_typ:
            return None
        return payload


@dataclass
class AuthBundle:
    disabled: bool
    token_auth: TokenAuth
    basic_auth: BasicAuth
    tokens: TokenService
    users: UserDirectory
    session: Session
    login: Login
    session_ttl: int


def load_auth_from_env() -> AuthBundle:
    disabled = os.environ.get("EDGE_AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes"}
    secret = os.environ.get("EDGE_SECRET_KEY", "").strip()
    if not secret and not disabled:
        secret = os.urandom(32).hex()
    access_ttl = int(os.environ.get("EDGE_TOKEN_TTL", "3600"))
    session_ttl = int(os.environ.get("EDGE_SESSION_TTL", str(access_ttl)))
    users = UserDirectory(os.environ.get("EDGE_USERS"))
    tokens = TokenService(secret, access_ttl=access_ttl)
    token_auth = TokenAuth()
    basic_auth = BasicAuth(realm="edge-microdot token issue")
    session = Session(secret_key=secret)
    login_mgr = Login(login_url="/login")
    return AuthBundle(
        disabled=disabled,
        token_auth=token_auth,
        basic_auth=basic_auth,
        tokens=tokens,
        users=users,
        session=session,
        login=login_mgr,
        session_ttl=session_ttl,
    )


def attach_auth_routes(app, auth: AuthBundle) -> None:
    auth.session.initialize(app, cookie_options={"max_age": auth.session_ttl, "path": "/", "http_only": True})
    auth.login.user_loader(load_user_factory(auth.users))

    @auth.token_auth.authenticate
    async def check_token(request, token):  # noqa: ARG001
        if auth.disabled:
            return EdgeUser("anonymous")
        return auth.tokens.verify_access(token)

    @auth.basic_auth.authenticate
    async def check_basic(request, username, password):  # noqa: ARG001
        if auth.disabled:
            return EdgeUser("anonymous")
        if auth.users.verify_password(username, password):
            return EdgeUser(username)
        return None

    @app.get("/login")
    async def login_form(request):
        from microdot import send_file as _send_file

        login_path = Path(__file__).resolve().parent / "static" / "login.html"
        return _send_file(str(login_path))

    @app.post("/login")
    @with_session
    async def login_submit(request, session):
        if auth.disabled:
            return redirect("/")
        body = request.json if request.content_type and "json" in request.content_type else {}
        if request.form:
            username = str(request.form.get("username", ""))
            password = str(request.form.get("password", ""))
        else:
            username = str(body.get("username") or "")
            password = str(body.get("password") or "")
        user = auth.users.get(username)
        if not user or not auth.users.verify_password(username, password):
            return Response("Invalid credentials", status_code=401)
        session["api_access"] = auth.tokens.issue_pair(user)["access_token"]
        return await auth.login.login_user(request, user, remember=False, redirect_url="/")

    @app.get("/logout")
    @with_session
    async def logout(request, session):
        await auth.login.logout_user(request)
        session.delete()
        return redirect("/login")

    @app.post("/auth/token")
    @auth.basic_auth
    async def auth_token(request):
        user = request.g.current_user
        return auth.tokens.issue_pair(user)

    @app.post("/auth/refresh")
    async def auth_refresh(request):
        if auth.disabled:
            return auth.tokens.issue_pair(EdgeUser("anonymous"))
        body = request.json or {}
        token = str(body.get("refresh_token") or "")
        pair = auth.tokens.refresh(token)
        if not pair:
            return Response("Invalid or expired refresh token", status_code=401)
        return pair

    @app.get("/auth/ws-token")
    @auth.login
    async def ws_token(request):
        user = await auth.login.get_current_user(request)
        return auth.tokens.issue_pair(user)

def hash_password(password: str, *, salt: str | None = None, iterations: int = 310_000) -> str:
    """Offline helper for EDGE_USERS entries."""
    import os as _os

    salt = salt or base64.b16encode(_os.urandom(8)).decode().lower()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    b64 = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"pbkdf2_sha256${iterations}${salt}${b64}"


def load_user_factory(users: UserDirectory):
    async def _load(user_id: str) -> EdgeUser | None:
        return users.get(user_id)

    return _load


def extract_bearer_token(request) -> str | None:
    header = request.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    return request.args.get("token")


def api_auth(auth: AuthBundle):
    """Apply TokenAuth only when auth is enabled (no Bearer header required when disabled)."""

    def decorator(f):
        if auth.disabled:
            async def passthrough(request, *args, **kwargs):
                request.g.current_user = EdgeUser("anonymous")
                return await f(request, *args, **kwargs)

            return passthrough
        return auth.token_auth(f)

    return decorator


def require_token(auth: AuthBundle, request) -> EdgeUser | Response:
    if auth.disabled:
        return EdgeUser("anonymous")
    token = extract_bearer_token(request)
    if not token:
        return Response("Unauthorized", status_code=401)
    user = auth.tokens.verify_access(token)
    if not user:
        return Response("Unauthorized", status_code=401)
    request.g.current_user = user
    return user
