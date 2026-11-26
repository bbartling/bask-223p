"""Microdot TokenAuth, BasicAuth, and session login (secrets from env only)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
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

LOG = logging.getLogger("edge.security")
_DUMMY_HASH = (
    "pbkdf2_sha256$310000$"
    "00112233445566778899aabbccddeeff$"
    "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA"
)


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
            self._pbkdf2(password, _DUMMY_HASH)
            return False
        return self._pbkdf2(password, digest)

    @staticmethod
    def _pbkdf2(password: str, digest: str) -> bool:
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


class TokenService:
    def __init__(self, secret: bytes | str, *, access_ttl: int = 3600, refresh_ttl: int | None = None) -> None:
        self.secret = secret
        self.access_ttl = max(60, int(access_ttl))
        self.refresh_ttl = refresh_ttl if refresh_ttl is not None else self.access_ttl * 12
        self.refresh_exp: dict[str, int] = {}

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
        refresh_jti = uuid.uuid4().hex
        refresh_exp = now + self.refresh_ttl
        self._prune_refresh(now)
        self.refresh_exp[refresh_jti] = refresh_exp
        refresh = jwt.encode(
            {
                "sub": user.username,
                "typ": "refresh",
                "iat": now,
                "exp": refresh_exp,
                "jti": refresh_jti,
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

    def issue_access(self, user: EdgeUser) -> dict[str, Any]:
        """Short-lived access token for the WebSocket. No refresh token."""
        now = int(time.time())
        expires = min(self.access_ttl, 300)
        expires = max(60, expires)
        access = jwt.encode(
            {
                "sub": user.username,
                "typ": "access",
                "iat": now,
                "exp": now + expires,
                "jti": uuid.uuid4().hex,
            },
            self.secret,
            algorithm="HS256",
        )
        return {"access_token": access, "token_type": "Bearer", "expires_in": expires}

    def refresh(self, refresh_token: str, users: UserDirectory | None = None) -> dict[str, Any] | None:
        payload = self._decode(refresh_token, expected_typ="refresh")
        if not payload:
            return None
        username = str(payload.get("sub") or "")
        if users is not None and users.get(username) is None:
            return None
        jti = str(payload.get("jti") or "")
        now = int(time.time())
        self._prune_refresh(now)
        if not jti or jti not in self.refresh_exp:
            return None
        del self.refresh_exp[jti]
        return self.issue_pair(EdgeUser(username))

    def _prune_refresh(self, now: int) -> None:
        expired = [jti for jti, exp in self.refresh_exp.items() if exp <= now]
        for jti in expired:
            del self.refresh_exp[jti]

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


def _subkey(secret: str, info: str) -> str:
    return hmac.new(secret.encode("utf-8"), info.encode("ascii"), hashlib.sha256).hexdigest()


def _read_secret() -> str:
    path = os.environ.get("EDGE_SECRET_KEY_FILE", "").strip()
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get("EDGE_SECRET_KEY", "").strip()


def _read_users() -> str | None:
    path = os.environ.get("EDGE_USERS_FILE", "").strip()
    if path:
        return Path(path).read_text(encoding="utf-8")
    return os.environ.get("EDGE_USERS")


def load_auth_from_env() -> AuthBundle:
    disabled = os.environ.get("EDGE_AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes"}
    secret = _read_secret()
    if not secret:
        raise RuntimeError("EDGE_SECRET_KEY is unset; refusing to start")
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError("EDGE_SECRET_KEY must be at least 32 bytes")
    if disabled:
        allow = os.environ.get("EDGE_ALLOW_UNAUTHENTICATED", "").strip().lower() in {"1", "true", "yes"}
        if not allow:
            raise RuntimeError("EDGE_AUTH_DISABLED requires EDGE_ALLOW_UNAUTHENTICATED=1")
        LOG.warning("authentication is disabled because EDGE_ALLOW_UNAUTHENTICATED=1")
    access_ttl = int(os.environ.get("EDGE_TOKEN_TTL", "3600"))
    session_ttl = int(os.environ.get("EDGE_SESSION_TTL", str(access_ttl)))
    users = UserDirectory(_read_users())
    tokens = TokenService(_subkey(secret, "edge-token"), access_ttl=access_ttl)
    token_auth = TokenAuth()
    basic_auth = BasicAuth(realm="edge-microdot token issue")
    session = Session(secret_key=_subkey(secret, "edge-session"))
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


_LOGIN_HITS: dict[str, list[float]] = {}


def login_limited(key: str, *, limit: int = 8, window: float = 60.0) -> bool:
    now = time.monotonic()
    recent = [stamp for stamp in _LOGIN_HITS.get(key, []) if now - stamp < window]
    if len(recent) >= limit:
        _LOGIN_HITS[key] = recent
        return True
    recent.append(now)
    _LOGIN_HITS[key] = recent
    return False


def client_addr(request) -> str:
    addr = getattr(request, "client_addr", None) or "local"
    if isinstance(addr, tuple):
        return str(addr[0])
    return str(addr)


def attach_auth_routes(app, auth: AuthBundle) -> None:
    auth.session.initialize(
        app,
        cookie_options={
            "max_age": auth.session_ttl,
            "path": "/",
            "http_only": True,
            "secure": True,
        },
    )
    auth.login.user_loader(load_user_factory(auth.users))

    # Microdot sets the session cookie in a request after-handler, which runs
    # after app after-handlers. Append SameSite once that cookie exists.
    _update_session = auth.session.update

    def _update_session_samesite(request, session):
        _update_session(request, session)

        @request.after_request
        async def _samesite(request, response):  # noqa: ARG001
            raw = response.headers.get("Set-Cookie")
            if isinstance(raw, list):
                response.headers["Set-Cookie"] = [
                    item if "samesite=" in item.lower() else item + "; SameSite=Strict" for item in raw
                ]
            elif isinstance(raw, str) and "samesite=" not in raw.lower():
                response.headers["Set-Cookie"] = [raw + "; SameSite=Strict"]
            return response

    auth.session.update = _update_session_samesite  # type: ignore[method-assign]

    _encode = auth.session.encode

    def _encode_exp(payload, secret_key=None):
        data = dict(payload)
        data.setdefault("exp", int(time.time()) + auth.session_ttl)
        return _encode(data, secret_key)

    auth.session.encode = _encode_exp  # type: ignore[method-assign]

    @auth.token_auth.authenticate
    async def check_token(request, token):  # noqa: ARG001
        if auth.disabled:
            return EdgeUser("anonymous")
        return auth.tokens.verify_access(token)

    @auth.basic_auth.authenticate
    async def check_basic(request, username, password):  # noqa: ARG001
        if auth.disabled:
            return EdgeUser("anonymous")
        if await asyncio.to_thread(auth.users.verify_password, username, password):
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
        if login_limited(f"form:{client_addr(request)}:{username}"):
            return Response("Too many attempts", status_code=429)
        ok = await asyncio.to_thread(auth.users.verify_password, username, password)
        user = auth.users.get(username) if ok else None
        if not user:
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
        pair = auth.tokens.refresh(token, auth.users)
        if not pair:
            return Response("Invalid or expired refresh token", status_code=401)
        return pair

    @app.get("/auth/ws-token")
    @auth.login
    async def ws_token(request):
        user = await auth.login.get_current_user(request)
        return auth.tokens.issue_access(user)

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
    """Bearer token from the Authorization header only. Never the query string."""
    header = request.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    return None


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
