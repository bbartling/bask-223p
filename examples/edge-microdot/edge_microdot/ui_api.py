"""Open-FDD UI REST contract 1.0.0 (open-fdd #1806 / #1807)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import jwt
from microdot import Response

from edge_microdot.fdd import RULE_FILE_IDS
from edge_microdot.role_contract import INPUT_NAME, ROLE_UNITS
from edge_microdot.security import client_addr, login_limited

CONTRACT_VERSION = "1.0.0"
ACCESS_TTL = 900
REFRESH_TTL = 60 * 60 * 12
_NAME = re.compile(INPUT_NAME)

# Stored SI symbols from units_si.py, spelled as the OpenAPI SiUnit enum.
_WIRE_UNIT = {
    "degC": ("°C", 1.0),
    "°C": ("°C", 1.0),
    "Δ°C": ("Δ°C", 1.0),
    "Δ°C/h": ("Δ°C/h", 1.0),
    "Pa": ("Pa", 1.0),
    "L/s": ("L/s", 1.0),
    "m3/s": ("L/s", 1000.0),
    "kW": ("kW", 1.0),
    "W": ("kW", 0.001),
    "kWh": ("kWh", 1.0),
    "J": ("kWh", 1.0 / 3_600_000.0),
    "%": ("%", 1.0),
    "ppm": ("ppm", 1.0),
    "h": ("h", 1.0),
    "": ("", 1.0),
}

_STATUS = {
    "PASS": "PASS",
    "FAULT": "FAULT",
    "METRIC": "METRIC",
    "ERROR": "ERROR",
    "INCONCLUSIVE": "NOT_ASSESSED",
    "SKIPPED": "NOT_ASSESSED",
    "STALE": "NOT_ASSESSED",
    "NOT_ASSESSED": "NOT_ASSESSED",
}


def local_rules() -> list[dict[str, Any]]:
    """Rules shipped with the edge app. confirm_min stays null until a bundle sets it."""
    titles = {
        "SV-STALE": "Stale series",
        "SV-RANGE": "Value outside range",
        "SV-FLATLINE": "Flat series",
        "SV-SPIKE": "Value spike",
    }
    return [
        {
            "rule_id": rule_id,
            "title": titles.get(rule_id, rule_id),
            "kind": "fault",
            "equipment_kinds": [],
            "inputs": [],
            "required": [],
            "confirm_min": None,
            "confirm_min_default": None,
        }
        for rule_id in sorted(set(RULE_FILE_IDS.values()))
    ]


def applicability_lines(bound_roles: set[str], rules: list[dict[str, Any]] | None = None) -> list[str]:
    lines: list[str] = []
    for rule in rules if rules is not None else local_rules():
        missing = [name for name in rule.get("required") or [] if name not in bound_roles]
        if missing:
            lines.append(f"{rule['rule_id']} skipped: missing {', '.join(missing)}")
        else:
            lines.append(f"{rule['rule_id']} applies")
    return lines


def _bound_roles(history) -> set[str]:
    rows = history.query("SELECT DISTINCT role FROM fdd.point WHERE role IS NOT NULL AND role <> ''")
    return {str(row[0]) for row in rows}


def _wire(unit: str | None, value: float | None) -> tuple[str, float | None]:
    symbol, scale = _WIRE_UNIT.get((unit or "").strip(), ("", 1.0))
    if value is None:
        return symbol, None
    return symbol, float(value) * scale


def _stamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(value).replace("+00:00", "Z")
    if text.endswith("Z"):
        head = text[:-1]
        if "." in head:
            head = head.split(".", 1)[0]
        return head + "Z"
    return text


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _error(code: str, message: str, status: int, **headers: str) -> Response:
    resp = Response({"error": {"code": code, "message": message}}, status_code=status, headers=headers or None)
    return resp


def _page(items: list[Any], limit: int, cursor: str | None) -> dict[str, Any] | Response:
    if cursor is None:
        offset = 0
    elif cursor.isdigit():
        offset = int(cursor)
    else:
        return _error("bad_request", "cursor is not valid", 400)
    page = items[offset : offset + limit]
    nxt = offset + limit
    return {"items": page, "next_cursor": str(nxt) if nxt < len(items) else None}


def _limit(request) -> int | Response:
    raw = request.args.get("limit")
    if raw is None or raw == "":
        return 100
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _error("bad_request", "limit must be an integer", 400)
    if isinstance(raw, str) and not raw.isdigit():
        return _error("bad_request", "limit must be an integer", 400)
    if value < 1 or value > 1000:
        return _error("bad_request", "limit must be from 1 to 1000", 400)
    return value


def attach_ui_api(app, *, history, auth, sparql, site: str) -> None:
    refresh_ids: dict[str, int] = {}
    tuned: dict[str, int] = {}
    secret = auth.tokens.secret

    def rules_write() -> bool:
        return os.environ.get("EDGE_RULES_WRITE", "").strip().lower() in {"1", "true", "yes"}

    def role_of(username: str) -> str:
        raw = os.environ.get("EDGE_ROLES", "")
        for part in raw.split(","):
            if ":" not in part:
                continue
            name, role = part.split(":", 1)
            if name.strip() == username and role.strip() in {"viewer", "operator", "admin"}:
                return role.strip()
        return "viewer"

    def remember_refresh(jti: str, exp: int) -> None:
        now = int(time.time())
        for old, old_exp in list(refresh_ids.items()):
            if old_exp <= now:
                del refresh_ids[old]
        refresh_ids[jti] = exp

    def issue(username: str, *, typ: str, ttl: int) -> tuple[str, str, int]:
        now = int(time.time())
        jti = uuid.uuid4().hex
        exp = now + ttl
        token = jwt.encode(
            {
                "sub": username,
                "role": role_of(username),
                "typ": typ,
                "iat": now,
                "exp": exp,
                "jti": jti,
            },
            secret,
            algorithm="HS256",
        )
        return token, jti, exp

    def decode(token: str, typ: str) -> dict[str, Any] | None:
        try:
            payload = jwt.decode(token, secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None
        if payload.get("typ") != typ:
            return None
        return payload

    def bearer(request) -> dict[str, Any] | Response:
        if auth.disabled:
            return {"sub": "anonymous", "role": "admin"}
        header = request.headers.get("Authorization") or ""
        if not header.lower().startswith("bearer "):
            return _error("unauthorized", "Authentication required", 401, **{"WWW-Authenticate": "Bearer"})
        payload = decode(header.split(" ", 1)[1].strip(), "ui-access")
        if not payload:
            return _error("unauthorized", "Authentication required", 401, **{"WWW-Authenticate": "Bearer"})
        return payload

    def cookie_value(request) -> str | None:
        raw = request.headers.get("Cookie") or ""
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith("ofdd_refresh="):
                return part.split("=", 1)[1]
        return None

    def refresh_cookie(resp: Response, token: str, *, max_age: int) -> Response:
        resp.headers["Set-Cookie"] = [
            f"ofdd_refresh={token}; Path=/api/auth; HttpOnly; Secure; SameSite=Strict; Max-Age={max_age}"
        ]
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def rule_rows() -> list[dict[str, Any]]:
        rows = []
        for rule in local_rules():
            current = tuned.get(rule["rule_id"], rule["confirm_min"])
            rows.append(
                {
                    "rule_id": rule["rule_id"],
                    "title": rule["title"],
                    "kind": rule["kind"],
                    "equipment_kinds": rule["equipment_kinds"],
                    "inputs": rule["inputs"],
                    "confirm_min": current,
                    "confirm_min_default": rule["confirm_min_default"],
                }
            )
        return rows

    def equipment_rows() -> list[dict[str, Any]]:
        found = history.query(
            """
            SELECT DISTINCT equipment_id, site_id
            FROM fdd.point
            WHERE equipment_id IS NOT NULL AND equipment_id <> ''
            ORDER BY equipment_id
            """
        )
        site_name = site or "site"
        return [
            {
                "id": str(eq),
                "name": str(eq),
                "kind": "general",
                "site": {"id": str(site_id or site_name), "name": str(site_id or site_name)},
                "parent_id": None,
            }
            for eq, site_id in found
        ]

    def point_rows(equipment: str | None) -> list[dict[str, Any]]:
        sql = """
            SELECT
                p.point_key, p.equipment_id, p.role, p.unit, p.kind,
                strftime(MAX(s.ts) AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%SZ'),
                arg_max(s.value, s.ts)
            FROM fdd.point p
            LEFT JOIN fdd.sample_store s
              ON s.site_id = p.site_id AND s.point_key = p.point_key
        """
        params: list[Any] = []
        if equipment:
            sql += " WHERE p.equipment_id = ?"
            params.append(equipment)
        sql += " GROUP BY p.point_key, p.equipment_id, p.role, p.unit, p.kind ORDER BY p.point_key"
        out = []
        for point_key, equipment_id, role, unit, kind, last_ts, last_value in history.query(sql, params):
            wire, value = _wire(unit, last_value)
            name = str(role or "")
            out.append(
                {
                    "id": str(point_key),
                    "name": str(point_key),
                    "equipment_id": str(equipment_id or "edge"),
                    "input": name if _NAME.match(name) and name in ROLE_UNITS else None,
                    "unit": wire,
                    "value_type": "boolean" if kind == "bool" else "number",
                    "last_sample_time": last_ts,
                    "last_value": value,
                }
            )
        return out

    @app.get("/api/meta")
    def meta(request):  # noqa: ARG001
        return {
            "contract_version": CONTRACT_VERSION,
            "app": {"name": "edge-microdot", "version": "0.1.0"},
            "capabilities": {
                "rules_write": rules_write(),
                "export_csv": False,
                "export_parquet": True,
                "sparql": bool(getattr(sparql, "enabled", False)),
                "auth": not auth.disabled,
            },
        }

    @app.get("/api/equipment")
    def equipment(request):
        user = bearer(request)
        if isinstance(user, Response):
            return user
        limit = _limit(request)
        if isinstance(limit, Response):
            return limit
        page = _page(equipment_rows(), limit, request.args.get("cursor"))
        return page

    @app.get("/api/points")
    def points(request):
        user = bearer(request)
        if isinstance(user, Response):
            return user
        limit = _limit(request)
        if isinstance(limit, Response):
            return limit
        return _page(point_rows(request.args.get("equipment")), limit, request.args.get("cursor"))

    @app.get("/api/samples")
    def samples(request):
        user = bearer(request)
        if isinstance(user, Response):
            return user
        point = request.args.get("point")
        start = _parse_time(request.args.get("from"))
        end = _parse_time(request.args.get("to"))
        if not point or start is None or end is None or start >= end:
            return _error("bad_request", "point, from, and to are required", 400)
        known = history.query("SELECT unit FROM fdd.point WHERE point_key = ? LIMIT 1", [point])
        if not known:
            return _error("not_found", "Unknown point", 404)
        limit = _limit(request)
        if isinstance(limit, Response):
            return limit
        unit, _scale = _wire(known[0][0], None)
        rows = history.query(
            """
            SELECT strftime(ts AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%SZ'), value
            FROM fdd.sample_store
            WHERE point_key = ? AND ts >= CAST(? AS TIMESTAMPTZ) AND ts < CAST(? AS TIMESTAMPTZ)
            ORDER BY ts ASC
            """,
            [point, start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")],
        )
        items = [{"t": _stamp(ts), "v": _wire(known[0][0], value)[1]} for ts, value in rows]
        page = _page(items, limit, request.args.get("cursor"))
        if isinstance(page, Response):
            return page
        page["point_id"] = point
        page["unit"] = unit
        return page

    @app.get("/api/results")
    def results(request):
        user = bearer(request)
        if isinstance(user, Response):
            return user
        start = _parse_time(request.args.get("from"))
        end = _parse_time(request.args.get("to"))
        if start is None or end is None or start >= end:
            return _error("bad_request", "from and to are required", 400)
        limit = _limit(request)
        if isinstance(limit, Response):
            return limit
        clauses = ["window_start < CAST(? AS TIMESTAMPTZ)", "window_end > CAST(? AS TIMESTAMPTZ)"]
        params: list[Any] = [
            end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ]
        if request.args.get("equipment"):
            clauses.append("equipment_id = ?")
            params.append(request.args.get("equipment"))
        if request.args.get("rule"):
            clauses.append("rule_id = ?")
            params.append(request.args.get("rule"))
        rows = history.query(
            f"""
            SELECT equipment_id, rule_id, subject_key, window_start, window_end, status, reason,
                   fault_hours, covered_hours, fault_pct, metrics
            FROM fdd.rule_result
            WHERE {' AND '.join(clauses)}
            ORDER BY window_start, equipment_id, rule_id, subject_key
            """,
            params,
        )
        items = []
        for eq, rule_id, subject, w0, w1, status, reason, fault, covered, pct, metrics in rows:
            mapped = _STATUS.get(str(status), "NOT_ASSESSED")
            assessed = mapped in {"PASS", "FAULT"}
            fault_h = float(fault) if assessed and fault is not None else (0.0 if mapped == "PASS" else None)
            covered_h = float(covered) if assessed and covered is not None else None
            if assessed and covered_h is not None and fault_h is not None and covered_h > 0:
                pct_out = round(100.0 * fault_h / covered_h, 6)
            elif assessed:
                pct_out = float(pct) if pct is not None else 0.0
            else:
                pct_out = None
            raw_metrics = metrics if isinstance(metrics, dict) else {}
            if isinstance(metrics, str):
                try:
                    raw_metrics = json.loads(metrics)
                except json.JSONDecodeError:
                    raw_metrics = {}
            items.append(
                {
                    "equipment_id": str(eq),
                    "rule_id": str(rule_id),
                    "subject_point_id": str(subject) if subject else None,
                    "window_start": _stamp(w0),
                    "window_end": _stamp(w1),
                    "status": mapped,
                    "reason": reason if mapped in {"NOT_ASSESSED", "ERROR"} else (reason or None),
                    "fault_hours": fault_h if assessed else None,
                    "covered_hours": covered_h if assessed else None,
                    "active_hours": None,
                    "fault_pct": pct_out if assessed else None,
                    "metrics": raw_metrics if mapped == "METRIC" else {},
                }
            )
        return _page(items, limit, request.args.get("cursor"))

    @app.get("/api/rules")
    def rules(request):
        user = bearer(request)
        if isinstance(user, Response):
            return user
        limit = _limit(request)
        if isinstance(limit, Response):
            return limit
        return _page(rule_rows(), limit, request.args.get("cursor"))

    @app.put("/api/rules/<rule_id>/confirm_min")
    def set_confirm(request, rule_id):
        user = bearer(request)
        if isinstance(user, Response):
            return user
        if not rules_write():
            return _error("capability_disabled", "rule writes are disabled on this host", 403)
        if user.get("role") not in {"operator", "admin"}:
            return _error("forbidden", "operator role is required", 403)
        found = next((row for row in rule_rows() if row["rule_id"] == rule_id), None)
        if found is None:
            return _error("not_found", "Unknown rule", 404)
        if found["confirm_min_default"] is None:
            return _error("not_tunable", "This rule has no confirmation delay", 409)
        try:
            body = request.json if isinstance(request.json, dict) else None
        except Exception:
            body = None
        if not isinstance(body, dict) or set(body) != {"confirm_min"}:
            return _error("bad_request", "Body must be confirm_min", 400)
        value = body["confirm_min"]
        if type(value) is not int or not 0 <= value <= 1440:
            return _error("bad_request", "confirm_min must be an integer from 0 to 1440", 400)
        tuned[rule_id] = value
        found["confirm_min"] = value
        return found

    def _auth_off():
        if not auth.disabled:
            return None
        return _error("not_found", "This host does not authenticate through /api/auth", 404)

    @app.post("/api/auth/login")
    async def login(request):
        off = _auth_off()
        if off:
            return off
        try:
            body = request.json if isinstance(request.json, dict) else None
        except Exception:
            body = None
        if not isinstance(body, dict) or set(body) != {"username", "password"}:
            return _error("bad_request", "username and password are required", 400)
        username = body["username"]
        password = body["password"]
        if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
            return _error("bad_request", "username and password are required", 400)
        if login_limited(f"ui:{client_addr(request)}:{username}"):
            return _error("rate_limited", "Too many attempts", 429)
        if not await asyncio.to_thread(auth.users.verify_password, username, password):
            return _error("invalid_credentials", "Invalid credentials", 401)
        access, _jti, _exp = issue(username, typ="ui-access", ttl=ACCESS_TTL)
        refresh, rid, refresh_exp = issue(username, typ="ui-refresh", ttl=REFRESH_TTL)
        remember_refresh(rid, refresh_exp)
        resp = Response(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TTL,
                "user": {"username": username, "role": role_of(username)},
            }
        )
        return refresh_cookie(resp, refresh, max_age=REFRESH_TTL)

    @app.post("/api/auth/refresh")
    def refresh(request):
        off = _auth_off()
        if off:
            return off
        token = cookie_value(request)
        payload = decode(token or "", "ui-refresh") if token else None
        if not payload or str(payload.get("jti")) not in refresh_ids:
            return _error("unauthorized", "Refresh token is not valid", 401)
        username = str(payload["sub"])
        if auth.users.get(username) is None:
            refresh_ids.pop(str(payload["jti"]), None)
            return _error("unauthorized", "Refresh token is not valid", 401)
        refresh_ids.pop(str(payload["jti"]), None)
        access, _jti, _exp = issue(username, typ="ui-access", ttl=ACCESS_TTL)
        new_refresh, rid, refresh_exp = issue(username, typ="ui-refresh", ttl=REFRESH_TTL)
        remember_refresh(rid, refresh_exp)
        resp = Response(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TTL,
                "user": {"username": username, "role": role_of(username)},
            }
        )
        return refresh_cookie(resp, new_refresh, max_age=REFRESH_TTL)

    @app.post("/api/auth/logout")
    def logout(request):
        off = _auth_off()
        if off:
            return off
        token = cookie_value(request)
        payload = decode(token or "", "ui-refresh") if token else None
        if payload:
            refresh_ids.pop(str(payload.get("jti")), None)
        resp = Response(b"", status_code=204)
        return refresh_cookie(resp, "", max_age=0)

    @app.errorhandler(404)
    def missing(request):
        if request.path.startswith("/api/"):
            return _error("not_found", "Not found", 404)
        return "Not found", 404
