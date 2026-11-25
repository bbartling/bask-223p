"""Read-only baskStream client and Niagara discovery snapshot loader.

Supports authenticated WebSocket (MessagePack) discovery against a Niagara 4
station running BASkStreamService (or the ``bask demo`` simulator), plus offline
recorded JSON snapshots. Only read-only operations are permitted; any attempt to
send a write/mutation op raises ``ValueError``.

Credentials are loaded from an env file or environment variables and are never
printed, logged, or serialized into output artifacts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import ssl
import time
import unicodedata
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from .haystack import load_env_credentials
from .refs import normalize_object_type, normalize_property_identifier
from .units import QUDT_UNIT_MAP, map_qudt_unit

READ_ONLY_OPS = frozenset({
    "ping",
    "capabilities",
    "browse",
    "describe",
    "search",
    "read",
    "subscribe",
    "unsubscribe",
    "subscription_status",
    "describe_component",
    "describe_component_types",
    "read_tags",
    "read_history",
    "read_history_rollup",
    "read_alarms",
    "read_schedule",
    "read_schedule_events",
    "resolve",
    "query",
})

_BACNET_OBJ_RE = re.compile(r"^([A-Za-z0-9_-]+)[: ,]+([0-9]+)$")

class BaskStreamError(RuntimeError):
    """Raised on Niagara SCRAM login, transport, or protocol errors."""


def _prep_username(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value)).replace("=", "=3D").replace(",", "=2C")


def _parse_scram(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in value.split(","):
        if "=" in part:
            key, val = part.split("=", 1)
            out[key] = val
    return out


def _xor_bytes(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def decode_n4_slot_text(text: str) -> str:
    return re.sub(r"\$([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text or "")


def parse_bacnet_object_id(encoded: str | None) -> tuple[str, int] | None:
    """Parse Niagara encoded BACnet objectId like ``analogInput:1173`` or ``device:5007``."""
    raw = (encoded or "").strip()
    if not raw:
        return None
    m = _BACNET_OBJ_RE.match(raw)
    if not m:
        return None
    obj_type = normalize_object_type(m.group(1))
    instance = int(m.group(2))
    return obj_type, instance


def parse_facets_string(encoded: str | None) -> dict[str, Any]:
    """Parse Niagara ``units=u:°F|precision=i:1`` encoded facets strings."""
    out: dict[str, Any] = {}
    raw = (encoded or "").strip()
    if not raw:
        return out
    for part in raw.split("|"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if ":" in v and len(v.split(":", 1)[0]) <= 2:
            prefix, val = v.split(":", 1)
            if prefix == "i" and val.lstrip("-").isdigit():
                out[k] = int(val)
            elif prefix in {"d", "f"}:
                try:
                    out[k] = float(val)
                except ValueError:
                    out[k] = val
            else:
                out[k] = val
        else:
            out[k] = v
    return out


def coerce_facets(value: Any) -> dict[str, Any]:
    """Normalize facets from any baskStream shape into a flat dict.

    Real stations return ``metadata.facets`` as a map (``{"units": "°F", "precision": "1"}``,
    enum ranges as option lists); ``describe_component`` slots carry the encoded string
    (``units=u:°F|precision=i:1``). Unknown shapes yield ``{}``.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(v, str) and v.lstrip("-").isdigit():
                out[str(k)] = int(v)
            else:
                out[str(k)] = v
        return out
    if isinstance(value, str):
        return parse_facets_string(value)
    return {}


class _CookieJar:
    def __init__(self) -> None:
        self._cookies: dict[str, str] = {}

    def store(self, response: Any) -> None:
        raw_values: list[str] = []
        try:
            raw_values.extend(response.raw.headers.getlist("Set-Cookie"))
        except Exception:
            pass
        fallback = response.headers.get("Set-Cookie")
        if fallback and fallback not in raw_values:
            raw_values.append(fallback)
        for raw in raw_values:
            try:
                parsed = SimpleCookie()
                parsed.load(raw)
                for name, morsel in parsed.items():
                    self._cookies[name] = morsel.value
            except Exception:
                first = raw.split(";", 1)[0]
                if "=" in first:
                    name, value = first.split("=", 1)
                    self._cookies[name.strip()] = value.strip()

    def header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())


class ReadOnlyBaskStreamClient:
    """Strictly read-only SCRAM-SHA-256 + MessagePack WebSocket client for baskStream."""

    def __init__(
        self,
        station: str,
        *,
        insecure: bool = False,
        timeout: int = 30,
        send_auth_header: bool = False,
        require_server_signature: bool = False,
    ) -> None:
        try:
            import msgpack  # noqa: F401
            import requests  # noqa: F401
            import websocket  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                'Live baskStream support requires msgpack, websocket-client, and requests: '
                'pip install "bask-223p[bask]"'
            ) from exc
        import requests

        self.station = station.rstrip("/")
        self.verify_tls = not insecure
        self.timeout = timeout
        self.send_auth_header = send_auth_header
        self.require_server_signature = require_server_signature
        self.server_signature_verified = False
        self.session = requests.Session()
        self.cookies = _CookieJar()
        self.ws: Any = None
        self.notices: list[dict[str, Any]] = []
        self.cov_events: list[dict[str, Any]] = []
        self._auth_header: str | None = None
        self.server_operations: set[str] | None = None
        self.api_version: str | None = None

    def _http(
        self,
        method: str,
        path: str,
        *,
        body: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        req_headers = {
            "User-Agent": "bask-223p/0.1",
            "Accept": "*/*",
        }
        cookie = self.cookies.header()
        if cookie:
            req_headers["Cookie"] = cookie
        if headers:
            req_headers.update(headers)
        if self._auth_header and "Authorization" not in req_headers:
            req_headers["Authorization"] = self._auth_header
        resp = self.session.request(
            method=method,
            url=self.station + path,
            data=body or b"",
            headers=req_headers,
            verify=self.verify_tls,
            allow_redirects=False,
            timeout=self.timeout,
        )
        self.cookies.store(resp)
        return resp

    def health(self) -> dict[str, Any] | None:
        resp = self._http("GET", "/stream/health")
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception as exc:
                raise BaskStreamError(f"/stream/health returned invalid JSON: {exc}") from exc
        return None

    def _basic_auth_header(self, username: str, password: str) -> str:
        tok = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return f"Basic {tok}"

    def login(self, username: str, password: str) -> dict[str, Any]:
        """Niagara web login. Tries form SCRAM-SHA-256 first, then HTTP Basic.

        Some Niagara 4.15 / JENEsys builds accept HTTP Basic on ``/stream/health``
        and the WebSocket even when the form SCRAM final message returns an empty
        or ``null`` body (no ``v=``). Credentials are never logged.
        """
        self.auth_mode = None
        if self.send_auth_header:
            self._auth_header = self._basic_auth_header(username, password)

        already = self.health()
        if already:
            self.auth_mode = "http-basic" if self._auth_header else "session"
            return already

        self._http("GET", "/prelogin")
        user_step = self._http(
            "POST",
            "/login",
            body=f"j_username={quote(username)}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if user_step.status_code != 200 or "j_security_check" not in user_step.text:
            raise BaskStreamError("Niagara username step failed (check user web access).")

        nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        client_first_bare = f"n={_prep_username(username)},r={nonce}"
        client_first = f"n,,{client_first_bare}"
        first = self._http(
            "POST",
            "/j_security_check/",
            body=f"action=sendClientFirstMessage&clientFirstMessage={client_first}",
            headers={"Content-Type": "application/x-niagara-login-support"},
        )
        if first.status_code != 200:
            raise BaskStreamError(f"SCRAM first message failed: HTTP {first.status_code}")

        server_first = first.text.strip()
        parsed = _parse_scram(server_first)
        if not parsed.get("r", "").startswith(nonce) or not parsed.get("s") or not parsed.get("i"):
            raise BaskStreamError("Invalid SCRAM server-first challenge")

        salted = hashlib.pbkdf2_hmac(
            "sha256",
            unicodedata.normalize("NFKC", password).encode("utf-8"),
            base64.b64decode(parsed["s"]),
            int(parsed["i"]),
            dklen=32,
        )
        client_final_no_proof = f"c=biws,r={parsed['r']}"
        auth_msg = f"{client_first_bare},{server_first},{client_final_no_proof}".encode("utf-8")
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        stored_key = hashlib.sha256(client_key).digest()
        client_sig = hmac.new(stored_key, auth_msg, hashlib.sha256).digest()
        proof_b64 = base64.b64encode(_xor_bytes(client_key, client_sig)).decode("ascii")

        final = self._http(
            "POST",
            "/j_security_check/",
            body=f"action=sendClientFinalMessage&clientFinalMessage={client_final_no_proof},p={proof_b64}",
            headers={"Content-Type": "application/x-niagara-login-support"},
        )
        final_text = final.text.strip()
        final_parsed = _parse_scram(final_text)
        scram_failed: str | None = None
        if final.status_code != 200:
            scram_failed = f"SCRAM final message failed: HTTP {final.status_code}"
        elif "e" in final_parsed:
            scram_failed = f"SCRAM server error: {final_parsed['e']}"
        elif "v" in final_parsed:
            server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
            expected_sig = base64.b64encode(hmac.new(server_key, auth_msg, hashlib.sha256).digest()).decode("ascii")
            if not hmac.compare_digest(final_parsed["v"], expected_sig):
                raise BaskStreamError("SCRAM server signature verification failed (possible impostor station)")
            self.server_signature_verified = True
        elif self.require_server_signature:
            raise BaskStreamError("SCRAM server-final message carried no server signature (v=)")

        if not scram_failed:
            self._http("GET", "/j_security_check/")
            health = self.health()
            if health:
                self.auth_mode = "scram"
                return health
            if final_text in {"", "null", "None"}:
                scram_failed = "SCRAM final message returned no server signature (empty/null body)"
            else:
                scram_failed = "/stream/health did not return 200 after Niagara SCRAM login"

        # Fall back to HTTP Basic for /stream/health and the WebSocket handshake.
        saved_header = self._auth_header
        self._auth_header = self._basic_auth_header(username, password)
        health = self.health()
        if health:
            self.auth_mode = "http-basic"
            return health
        self._auth_header = saved_header
        raise BaskStreamError(
            f"{scram_failed}; HTTP Basic fallback for /stream/health also failed"
        )

    def connect(self) -> None:
        import websocket

        cookie_header = self.cookies.header()
        if not cookie_header and not self._auth_header:
            raise BaskStreamError("No Niagara session cookies or Authorization header available; call login() first.")
        parsed = urlparse(self.station)
        if parsed.scheme == "https":
            ws_url = f"wss://{parsed.netloc}/stream"
        elif parsed.scheme == "http":
            ws_url = f"ws://{parsed.netloc}/stream"
        else:
            raise ValueError("Station URL must start with http:// or https://")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        headers = [f"Cookie: {cookie_header}"]
        if self._auth_header:
            headers.append(f"Authorization: {self._auth_header}")
        sslopt = (
            {"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True}
            if self.verify_tls
            else {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
        )
        self.ws = websocket.create_connection(
            ws_url,
            header=headers,
            origin=origin,
            sslopt=sslopt,
            timeout=self.timeout,
        )

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            finally:
                self.ws = None
        self.session.close()

    def __enter__(self) -> "ReadOnlyBaskStreamClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def call(self, op: str, **fields: Any) -> dict[str, Any]:
        if op not in READ_ONLY_OPS:
            raise ValueError(f"Write or unsupported operation forbidden in read-only client: {op!r}")
        if self.ws is None:
            raise BaskStreamError("WebSocket is not connected.")
        import msgpack
        import websocket

        req_id = fields.pop("id", f"{op}-{uuid.uuid4().hex[:8]}")
        frame: dict[str, Any] = {"op": op, "id": req_id}
        frame.update(fields)
        self.ws.send(msgpack.packb(frame, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)

        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BaskStreamError(f"{op} timed out waiting for response id={req_id}")
            self.ws.settimeout(remaining)
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException as exc:
                raise BaskStreamError(f"{op} timed out waiting for response id={req_id}") from exc
            if isinstance(raw, str):
                continue
            msg = msgpack.unpackb(raw, raw=False)
            if not isinstance(msg, dict):
                continue
            if msg.get("id") != req_id:
                msg_op = msg.get("op")
                if msg_op in {"session_revoked", "request_timeout"}:
                    raise BaskStreamError(f"Server terminated session ({msg_op}): {msg.get('reason') or ''}")
                if msg_op == "cov":
                    self.cov_events.append(msg)
                else:
                    self.notices.append(msg)
                continue
            if msg.get("error") or msg.get("op") == "error":
                raise BaskStreamError(f"{op} failed: {msg.get('code') or 'error'}: {msg.get('message') or msg}")
            return msg

    def ping(self) -> dict[str, Any]:
        return self.call("ping")

    def capabilities(self) -> dict[str, Any]:
        result = self.call("capabilities")
        caps = result.get("capabilities") or {}
        ops = caps.get("operations")
        self.server_operations = set(ops) if isinstance(ops, list) and ops else None
        self.api_version = str(caps.get("apiVersion") or "") or None
        return result

    def browse(self, base: str, *, depth: int = 1, metadata: str = "full") -> dict[str, Any]:
        return self.call("browse", base=base, depth=depth, metadata=metadata)

    def describe(self, ord_value: str, *, metadata: str = "full") -> dict[str, Any]:
        return self.call("describe", ord=ord_value, metadata=metadata)

    def describe_component(self, ord_value: str) -> dict[str, Any]:
        return self.call("describe_component", ord=ord_value)

    def search(
        self,
        base: str,
        *,
        query: str = "",
        features: list[str] | None = None,
        metadata: str = "full",
        depth: int = 32,
        limit: int = 1000,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "base": base,
            "query": query,
            "depth": depth,
            "metadata": metadata,
            "limit": limit,
        }
        if features:
            payload["features"] = features
        return self.call("search", **payload)

    def read(self, points: list[str]) -> dict[str, Any]:
        return self.call("read", points=points)

    def subscribe(self, points: list[str]) -> dict[str, Any]:
        """Subscribe to read-only COV point updates (API 1.12+)."""
        return self.call("subscribe", points=list(points))

    def unsubscribe(self, points: list[str] | None = None) -> dict[str, Any]:
        """Unsubscribe from COV updates for specific points or all points."""
        if points is None:
            return self.call("unsubscribe")
        return self.call("unsubscribe", points=list(points))

    def subscription_status(self) -> dict[str, Any]:
        """Return active COV subscription count and limits."""
        return self.call("subscription_status")

    def recv_event(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        """Wait for the next unsolicited COV frame or server notice, or None on timeout."""
        if self.cov_events:
            return self.cov_events.pop(0)
        if self.ws is None:
            raise BaskStreamError("WebSocket is not connected.")
        import msgpack
        import websocket

        wait_s = self.timeout if timeout is None else timeout
        self.ws.settimeout(wait_s)
        try:
            raw = self.ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        if isinstance(raw, str):
            return None
        msg = msgpack.unpackb(raw, raw=False)
        if not isinstance(msg, dict):
            return None
        msg_op = msg.get("op")
        if msg_op in {"session_revoked", "request_timeout"}:
            raise BaskStreamError(f"Server terminated session ({msg_op}): {msg.get('reason') or ''}")
        if msg_op != "cov":
            self.notices.append(msg)
        return msg

    def read_tags(
        self, ords: str | list[str], *, dictionary: str | None = None, include_relations: bool = True
    ) -> dict[str, Any]:
        """``read_tags`` for one ORD or up to 100 ORDs (API 1.5+)."""
        targets = [ords] if isinstance(ords, str) else list(ords)
        if not targets or len(targets) > 100:
            raise ValueError("read_tags takes 1..100 ORDs per request")
        payload: dict[str, Any] = {"ords": targets, "includeRelations": include_relations}
        if dictionary:
            payload["dictionary"] = dictionary
        return self.call("read_tags", **payload)

    def resolve(self, equip: str, *, role: str | None = None, tags: list[str] | None = None) -> dict[str, Any]:
        """API 1.14 ``resolve``: the station's inferred point for an equipment + role/tags.

        The answer is a *suggestion* (``inferred: true``); callers must treat it as a
        review candidate, never as an approved semantic binding.
        """
        if not role and not tags:
            raise ValueError("resolve needs a role or tags")
        self._require_op("resolve", "1.14")
        payload: dict[str, Any] = {"equip": equip}
        if role:
            payload["role"] = role
        if tags:
            payload["tags"] = list(tags)
        return self.call("resolve", **payload)

    def supports(self, op: str) -> bool:
        """True when the last ``capabilities`` answer listed ``op`` (unknown -> True)."""
        ops = self.server_operations
        return True if ops is None else op in ops

    def _require_op(self, op: str, since: str) -> None:
        """Fail early, without a round trip, for an op the station did not advertise."""
        if getattr(self, "server_operations", None) is not None and op not in self.server_operations:
            have = getattr(self, "api_version", None) or "unknown"
            raise BaskStreamError(f"unsupported_op: {op} needs baskStream API {since}+ (station reports {have})")


def _slots_map(comp_result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = comp_result.get("result") if "result" in comp_result else comp_result
    out: dict[str, dict[str, Any]] = {}
    for slot in (result or {}).get("slots") or []:
        name = slot.get("name")
        if name:
            out[str(name)] = slot
    return out


def _slot_encoded(slot: dict[str, Any] | None) -> str:
    if not slot:
        return ""
    val = slot.get("value")
    if isinstance(val, dict):
        if "encoded" in val:
            return str(val["encoded"])
        if "value" in val:
            return str(val["value"])
    if val is None:
        return ""
    return str(val)


def _extract_device_bacnet(client: ReadOnlyBaskStreamClient, device_ord: str) -> tuple[int | None, str]:
    """BACnet device instance and BACnet device name (``objectName``), read-only.

    Tries the device component, then ``config/deviceObject`` (where a Niagara
    ``bacnet:BacnetDevice`` keeps the remote Device object's ``objectId`` and
    ``objectName``). The Niagara display name is *not* returned as a BACnet name.
    """
    instance: int | None = None
    bacnet_name = ""
    for target in (device_ord, f"{device_ord}/config/deviceObject"):
        try:
            comp = client.describe_component(target)
        except BaskStreamError:
            continue
        slots = _slots_map(comp)
        if instance is None and "objectId" in slots:
            parsed = parse_bacnet_object_id(_slot_encoded(slots["objectId"]))
            if parsed and parsed[0] == "device":
                instance = parsed[1]
        if not bacnet_name and "objectName" in slots:
            bacnet_name = _slot_encoded(slots["objectName"])
        if instance is not None and bacnet_name:
            break
    return instance, bacnet_name


def _extract_point_proxy_bacnet(
    client: ReadOnlyBaskStreamClient, point_ord: str
) -> dict[str, Any]:
    """Read BACnet proxyExt objectId, propertyId, and write priority from point or proxyExt."""
    info: dict[str, Any] = {}
    for target in (f"{point_ord}/proxyExt", point_ord):
        try:
            comp = client.describe_component(target)
        except BaskStreamError:
            continue
        res_node = comp.get("result") if isinstance(comp.get("result"), dict) else {}
        ext_type = str(res_node.get("typeSpec") or comp.get("typeSpec") or "").strip()
        if target.endswith("/proxyExt") and ext_type and "proxy_ext_type" not in info:
            info["proxy_ext_type"] = ext_type
        slots = _slots_map(comp)
        if "objectId" in slots and "object_id" not in info:
            parsed = parse_bacnet_object_id(_slot_encoded(slots["objectId"]))
            if parsed:
                info["object_type"], info["object_instance"] = parsed
                info["object_id"] = f"{parsed[0]},{parsed[1]}"
        if "propertyId" in slots and "property_identifier" not in info:
            raw_prop = _slot_encoded(slots["propertyId"])
            if raw_prop:
                info["property_identifier"] = normalize_property_identifier(raw_prop)
        if "propertyArrayIndex" in slots and "property_array_index" not in info:
            raw_idx = _slot_encoded(slots["propertyArrayIndex"])
            if raw_idx.isdigit():
                info["property_array_index"] = int(raw_idx)
        if "writePriority" in slots and "priority_for_writing" not in info:
            raw_pri = _slot_encoded(slots["writePriority"])
            if raw_pri.isdigit() and 1 <= int(raw_pri) <= 16:
                info["priority_for_writing"] = int(raw_pri)
        if "facets" in slots and "facets_encoded" not in info:
            info["facets_encoded"] = _slot_encoded(slots["facets"])
    return info


def _value_kind_from_node(node: dict[str, Any]) -> str:
    meta = node.get("metadata") or {}
    write_kind = str((meta.get("write") or {}).get("valueKind") or "").casefold()
    if write_kind in {"numeric", "boolean", "enum", "string"}:
        return write_kind
    ts = str(node.get("typeSpec") or "").casefold()
    if "numeric" in ts:
        return "numeric"
    if "boolean" in ts:
        return "boolean"
    if "enum" in ts:
        return "enum"
    return "numeric"



# Immediate children commonly present under slot:/Drivers. Used only when the
# configured base itself is blocked by allowedPathPatterns (e.g. slot:/Drivers/*
# does not match slot:/Drivers).
_DRIVER_CHILD_CANDIDATES = (
    "BacnetNetwork",
    "NiagaraNetwork",
    "ModbusTcpNetwork",
    "ModbusAsyncNetwork",
    "LonNetwork",
    "SnmpNetwork",
    "FEC_Proxys",
    "NF_Proxies",
)


def _collect_discovery_nodes(
    client: ReadOnlyBaskStreamClient,
    base: str,
    limit: int,
    warnings: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Browse/search ``base``, or its immediate children if the parent is forbidden."""
    extra_folders: list[str] = []
    try:
        root_browse = (client.browse(base, depth=1, metadata="full") or {}).get("node") or {}
        search_res = (client.search(base, metadata="full", limit=limit) or {}).get("result") or {}
        all_nodes = list(search_res.get("nodes") or [])
        if search_res.get("truncated"):
            warnings.append(
                f"search under {base} truncated ({','.join(search_res.get('truncatedReasons') or []) or 'unknown'}); "
                "raise --limit or narrow --slot-base"
            )
        extra_folders.append(base)
        return root_browse, all_nodes, extra_folders
    except BaskStreamError as exc:
        msg = str(exc)
        if "forbidden_point" not in msg:
            raise
        warnings.append(
            f"{base} is outside allowedPathPatterns (the parent folder is not included). "
            "On the station, set Allowed Path Patterns to include the folder itself, "
            "e.g. slot:/Drivers or slot:/Drivers/** (slot:/Drivers/* matches children only)."
        )

    root_browse: dict[str, Any] = {}
    all_nodes: list[dict[str, Any]] = []
    found: list[str] = []
    for name in _DRIVER_CHILD_CANDIDATES:
        child = f"{base.rstrip('/')}/{name}"
        try:
            node = (client.browse(child, depth=1, metadata="full") or {}).get("node") or {}
        except BaskStreamError:
            continue
        found.append(child)
        extra_folders.append(child)
        all_nodes.append(node)
        all_nodes.extend(node.get("children") or [])
        try:
            search_res = (client.search(child, metadata="full", limit=limit) or {}).get("result") or {}
            all_nodes.extend(search_res.get("nodes") or [])
            if search_res.get("truncated"):
                warnings.append(f"search under {child} truncated")
        except BaskStreamError as exc:
            warnings.append(f"search under {child} failed: {exc}")
    if not found:
        raise BaskStreamError(
            f"{base} is forbidden and no well-known child folders were readable. "
            "Widen allowedPathPatterns or pass --slot-base at a child such as slot:/Drivers/BacnetNetwork."
        )
    warnings.append("Discovered under: " + ", ".join(found))
    return root_browse, all_nodes, extra_folders



def _is_exportable_point(pt: dict[str, Any]) -> bool:
    """True for Niagara control points; skip proxyExt children and timers."""
    ts = str(pt.get("typeSpec") or "").casefold()
    if "proxyext" in ts or "timetrigger" in ts:
        return False
    kind = str(pt.get("kind") or "").casefold()
    if kind == "point":
        return True
    cls = ((pt.get("metadata") or {}).get("classification") or {})
    if cls.get("isProxyPoint") or cls.get("isPoint") or cls.get("isWritablePoint"):
        return True
    name = ts.rsplit(":", 1)[-1]
    return name.endswith("point") or name.endswith("writable")


def _point_entry(client: ReadOnlyBaskStreamClient, pt: dict[str, Any], pt_ord: str) -> dict[str, Any]:
    if "metadata" not in pt:
        pt = (client.describe(pt_ord, metadata="full") or {}).get("node") or pt
    meta = pt.get("metadata") or {}
    pt_meta = meta.get("point") or {}
    facets = coerce_facets(meta.get("facets") or pt_meta.get("facets"))
    proxy = _extract_point_proxy_bacnet(client, pt_ord)
    if not facets and proxy.get("facets_encoded"):
        facets = parse_facets_string(proxy["facets_encoded"])

    writable = bool(pt.get("writable") or pt_meta.get("writable"))
    active_ord = pt_meta.get("activeLevelOrdinal")
    write_pri = proxy.get("priority_for_writing")
    if write_pri is None and writable and isinstance(active_ord, int) and 1 <= active_ord <= 16:
        write_pri = active_ord

    tags_raw = meta.get("tags") or []
    hs_tags: list[str] = []
    for t in tags_raw:
        tid = str(t.get("id") or "")
        if tid.startswith("hs:"):
            hs_tags.append(tid.split(":", 1)[1])

    pt_entry: dict[str, Any] = {
        "ord": pt_ord,
        "name": str(pt.get("name") or ""),
        "display": str(pt.get("display") or pt.get("name") or ""),
        "typeSpec": str(pt.get("typeSpec") or ""),
        "valueKind": _value_kind_from_node(pt),
        "writable": writable,
        "facets": facets,
        "haystackTags": sorted(set(hs_tags)),
        "tags": tags_raw,
    }
    if "object_type" in proxy and "object_instance" in proxy:
        pt_entry["bacnet"] = {
            "object_type": proxy["object_type"],
            "object_instance": proxy["object_instance"],
            "object_identifier": proxy["object_id"],
            "property_identifier": proxy.get("property_identifier", "present-value"),
        }
        if "property_array_index" in proxy:
            pt_entry["bacnet"]["property_array_index"] = proxy["property_array_index"]
        if write_pri is not None:
            pt_entry["bacnet"]["priority_for_writing"] = write_pri
    elif write_pri is not None:
        pt_entry["priority_for_writing"] = write_pri
    return pt_entry


def _append_folder_points(
    client: ReadOnlyBaskStreamClient,
    devices_out: list[dict[str, Any]],
    folders: list[str],
    limit: int,
    warnings: list[str],
) -> None:
    """Add points that live under a Drivers child folder but not under a BacnetDevice."""
    seen = {p["ord"] for d in devices_out for p in d["points"]}
    for folder in folders:
        try:
            pt_search = (client.search(folder, features=["point"], metadata="full", limit=limit) or {}).get("result") or {}
        except BaskStreamError as exc:
            warnings.append(f"point search under {folder} failed: {exc}")
            continue
        leftover: list[dict[str, Any]] = []
        for pt in pt_search.get("nodes") or []:
            pt_ord = str(pt.get("slotPath") or pt.get("ord") or "")
            if not pt_ord or pt_ord in seen:
                continue
            if not _is_exportable_point(pt):
                continue
            leftover.append(_point_entry(client, pt, pt_ord))
            seen.add(pt_ord)
        if not leftover:
            continue
        try:
            node = (client.describe(folder, metadata="full") or {}).get("node") or {}
        except BaskStreamError:
            node = {}
        devices_out.append({
            "ord": folder,
            "name": str(node.get("display") or node.get("name") or folder.rsplit("/", 1)[-1]),
            "typeSpec": str(node.get("typeSpec") or ""),
            "device_instance": None,
            "bacnet_device_name": "",
            "tags": (node.get("metadata") or {}).get("tags") or [],
            "points": leftover,
        })


def discover_station_snapshot(
    station: str,
    *,
    base: str = "slot:/Drivers",
    env_file: Path | None = None,
    username: str | None = None,
    password: str | None = None,
    insecure: bool = False,
    limit: int = 1000,
    ws_auth_header: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    """Run read-only baskStream discovery and return a normalized JSON snapshot.

    ``ws_auth_header`` sends an ``Authorization`` header on the WebSocket handshake,
    needed when the service has ``requireAuthorizationHeader=true``.

    The snapshot never includes credentials, cookies, or Authorization headers. It
    records ``warnings`` (truncated searches, server notices, unbound points) so a
    partial discovery is never silently treated as complete.
    """
    if not username or not password:
        env_u, env_p = load_env_credentials(env_file)
        username = username or env_u
        password = password or env_p
    if not username or not password:
        raise ValueError("Station credentials required (provide --env-file or NIAGARA_USER/NIAGARA_PASS)")

    warnings: list[str] = []
    with ReadOnlyBaskStreamClient(
        station, insecure=insecure, timeout=timeout, send_auth_header=ws_auth_header
    ) as client:
        health = client.login(username, password)
        if getattr(client, "auth_mode", None) == "http-basic":
            warnings.append(
                "Niagara form SCRAM did not establish a session; logged in to /stream with HTTP Basic"
            )
        client.connect()
        client.ping()
        caps = (client.capabilities() or {}).get("capabilities") or {}

        root_browse, all_nodes, extra_folders = _collect_discovery_nodes(client, base, limit, warnings)

        device_nodes: list[dict[str, Any]] = []
        seen_devs: set[str] = set()
        for candidate in [root_browse] + list(root_browse.get("children") or []) + all_nodes:
            ord_val = str(candidate.get("slotPath") or candidate.get("ord") or "")
            if not ord_val or ord_val in seen_devs:
                continue
            ts = str(candidate.get("typeSpec") or "")
            cls = ((candidate.get("metadata") or {}).get("classification") or {})
            if cls.get("isDriverDevice") or ts.endswith("BacnetDevice"):
                seen_devs.add(ord_val)
                device_nodes.append(candidate)

        devices_out: list[dict[str, Any]] = []
        for dev in device_nodes:
            dev_ord = str(dev.get("slotPath") or dev.get("ord") or "")
            desc_node = (client.describe(dev_ord, metadata="full") or {}).get("node") or dev
            dev_instance, dev_bacnet_name = _extract_device_bacnet(client, dev_ord)
            dev_name = str(desc_node.get("display") or desc_node.get("name") or dev_ord.rsplit("/", 1)[-1])

            pt_search = (client.search(dev_ord, features=["point"], metadata="full", limit=limit) or {}).get("result") or {}
            pt_nodes = pt_search.get("nodes") or []
            if pt_search.get("truncated"):
                warnings.append(f"point search under {dev_ord} truncated")

            points_out: list[dict[str, Any]] = []
            for pt in pt_nodes:
                pt_ord = str(pt.get("slotPath") or pt.get("ord") or "")
                if not pt_ord:
                    continue
                if not _is_exportable_point(pt):
                    continue
                if "metadata" not in pt:
                    pt = (client.describe(pt_ord, metadata="full") or {}).get("node") or pt
                meta = pt.get("metadata") or {}
                pt_meta = meta.get("point") or {}
                facets = coerce_facets(meta.get("facets") or pt_meta.get("facets"))
                proxy = _extract_point_proxy_bacnet(client, pt_ord)
                if not facets and proxy.get("facets_encoded"):
                    facets = parse_facets_string(proxy["facets_encoded"])

                writable = bool(pt.get("writable") or pt_meta.get("writable"))
                active_ord = pt_meta.get("activeLevelOrdinal")
                write_pri = proxy.get("priority_for_writing")
                if write_pri is None and writable and isinstance(active_ord, int) and 1 <= active_ord <= 16:
                    write_pri = active_ord

                tags_raw = meta.get("tags") or []
                hs_tags: list[str] = []
                for t in tags_raw:
                    tid = str(t.get("id") or "")
                    if tid.startswith("hs:"):
                        hs_tags.append(tid.split(":", 1)[1])

                pt_entry: dict[str, Any] = {
                    "ord": pt_ord,
                    "name": str(pt.get("name") or ""),
                    "display": str(pt.get("display") or pt.get("name") or ""),
                    "typeSpec": str(pt.get("typeSpec") or ""),
                    "valueKind": _value_kind_from_node(pt),
                    "writable": writable,
                    "facets": facets,
                    "haystackTags": sorted(set(hs_tags)),
                    "tags": tags_raw,
                }
                if proxy.get("proxy_ext_type"):
                    pt_entry["proxyExtType"] = proxy["proxy_ext_type"]
                if "object_type" in proxy and "object_instance" in proxy:
                    pt_entry["bacnet"] = {
                        "object_type": proxy["object_type"],
                        "object_instance": proxy["object_instance"],
                        "object_identifier": proxy["object_id"],
                        "property_identifier": proxy.get("property_identifier", "present-value"),
                    }
                    if "property_array_index" in proxy:
                        pt_entry["bacnet"]["property_array_index"] = proxy["property_array_index"]
                    if write_pri is not None:
                        pt_entry["bacnet"]["priority_for_writing"] = write_pri
                elif write_pri is not None:
                    pt_entry["priority_for_writing"] = write_pri
                points_out.append(pt_entry)

            devices_out.append({
                "ord": dev_ord,
                "name": dev_name,
                "typeSpec": str(desc_node.get("typeSpec") or ""),
                "device_instance": dev_instance,
                "bacnet_device_name": dev_bacnet_name,
                "tags": (desc_node.get("metadata") or {}).get("tags") or [],
                "points": points_out,
            })

        _append_folder_points(client, devices_out, extra_folders, limit, warnings)

        sig_verified = bool(getattr(client, "server_signature_verified", False))
        for notice in getattr(client, "notices", None) or []:
            warnings.append(f"server notice: {notice.get('op')} {notice.get('reason') or ''}".strip())

    unbound = sum(1 for d in devices_out for p in d["points"] if "bacnet" not in p)
    if unbound:
        warnings.append(f"{unbound} point(s) have no BACnet proxyExt objectId (left unbound)")
    return {
        "format": "baskstream-discovery-snapshot",
        "version": 1,
        "station": station,
        "base": base,
        "apiVersion": str(caps.get("apiVersion") or health.get("apiVersion") or ""),
        "module": str(caps.get("module") or "baskStream"),
        "writesEnabled": caps.get("writesEnabled"),
        "scramServerSignatureVerified": sig_verified,
        "warnings": warnings,
        "devices": devices_out,
    }


def load_baskstream_snapshot(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
        raise ValueError(f"{path} is not a baskstream-discovery-snapshot JSON (expected 'devices' list)")
    return payload
