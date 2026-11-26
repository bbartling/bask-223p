"""Token-authenticated BACnet routes. Handlers run on Microdot's loop.

The BACpypes3 application is created on that same loop (poll tasks included).
Do not call these coroutines from a second loop.
"""
from __future__ import annotations

import os
from typing import Any

from microdot import Response

from edge_microdot.security import api_auth
from edge_microdot.sources.bacnet_rpm import (
    BacnetTransportError,
    PriorityRejected,
    WriteDisabled,
)


def _read_cap() -> int:
    raw = os.environ.get("EDGE_BACNET_READ_MAX", "50").strip()
    try:
        cap = int(raw)
    except ValueError:
        cap = 50
    return cap if cap > 0 else 50


def _error(code: str, message: str, status: int) -> Response:
    return Response({"error": {"code": code, "message": message}}, status_code=status)


def _role(username: str) -> str:
    raw = os.environ.get("EDGE_ROLES", "")
    for part in raw.split(","):
        if ":" not in part:
            continue
        name, role = part.split(":", 1)
        if name.strip() == username and role.strip() in {"viewer", "operator", "admin"}:
            return role.strip()
    return "operator"


def _user(request) -> str:
    current = getattr(getattr(request, "g", None), "current_user", None)
    if current is None:
        return ""
    return str(getattr(current, "username", "") or getattr(current, "id", "") or "")


def _bacnet(source) -> Any | Response:
    if source is None or not hasattr(source, "discover_points") or not hasattr(source, "who_is"):
        return _error("bacnet_inactive", "BACnet driver is not active", 503)
    return source


def _int_arg(value: str | None, name: str) -> int | Response:
    if value is None or value == "":
        return _error("bad_request", f"{name} is required", 400)
    try:
        return int(value)
    except (TypeError, ValueError):
        return _error("bad_request", f"{name} must be an integer", 400)


def attach_bacnet_api(app, *, source, auth) -> None:
    protect = api_auth(auth)

    @app.get("/api/bacnet/whois")
    @protect
    async def whois(request):
        src = _bacnet(source)
        if isinstance(src, Response):
            return src
        low = _int_arg(request.args.get("low"), "low")
        if isinstance(low, Response):
            return low
        high = _int_arg(request.args.get("high"), "high")
        if isinstance(high, Response):
            return high
        timeout = request.args.get("timeout")
        try:
            timeout_s = float(timeout) if timeout else None
            devices = await src.who_is(
                low_limit=low,
                high_limit=high,
                address=request.args.get("address") or None,
                timeout=timeout_s,
            )
        except ValueError as exc:
            return _error("bad_request", str(exc), 400)
        except BacnetTransportError as exc:
            return _error("bacnet_error", str(exc), 502)
        return {"low": low, "high": high, "devices": [dev.to_dict() for dev in devices]}

    @app.get("/api/bacnet/devices")
    @protect
    async def named_devices(request):
        src = _bacnet(source)
        if isinstance(src, Response):
            return src
        found = []
        for inst in list(getattr(src, "discover_instances", []) or []):
            try:
                rows = await src.who_is(low_limit=int(inst), high_limit=int(inst))
            except BacnetTransportError as exc:
                found.append({"device_instance": int(inst), "error": str(exc)})
                continue
            match = next((dev for dev in rows if dev.device_instance == int(inst)), None)
            if match is None:
                found.append({"device_instance": int(inst), "error": "no I-Am"})
                continue
            info = match.to_dict()
            # I-Am has no object-name. One ReadProperty is cheap for a named device.
            if not info.get("object_name"):
                try:
                    rows_name = await src.read_properties(
                        int(inst),
                        [(f"device,{int(inst)}", "object-name")],
                        address=match.address,
                    )
                except BacnetTransportError:
                    rows_name = []
                if rows_name and rows_name[0].get("status") == "ok" and rows_name[0].get("value"):
                    info["object_name"] = str(rows_name[0]["value"])
                    match.object_name = info["object_name"]
            found.append(info)
        return {"devices": found}

    @app.get("/api/bacnet/devices/<instance>")
    @protect
    async def device_info(request, instance):
        src = _bacnet(source)
        if isinstance(src, Response):
            return src
        parsed = _int_arg(instance, "instance")
        if isinstance(parsed, Response):
            return parsed
        try:
            return await src.read_device_info(parsed, address=request.args.get("address") or None)
        except BacnetTransportError as exc:
            return _error("not_found", str(exc), 404)

    @app.get("/api/bacnet/devices/<instance>/objects")
    @protect
    async def device_objects(request, instance):
        src = _bacnet(source)
        if isinstance(src, Response):
            return src
        parsed = _int_arg(instance, "instance")
        if isinstance(parsed, Response):
            return parsed
        try:
            return await src.discover_points(parsed, address=request.args.get("address") or None)
        except BacnetTransportError as exc:
            return _error("bacnet_error", str(exc), 502)

    @app.get("/api/bacnet/priority")
    @protect
    async def priority(request):
        src = _bacnet(source)
        if isinstance(src, Response):
            return src
        dev = _int_arg(request.args.get("device"), "device")
        if isinstance(dev, Response):
            return dev
        obj = (request.args.get("object") or "").strip()
        if not obj:
            return _error("bad_request", "object is required", 400)
        try:
            return await src.read_priority_array(dev, obj, address=request.args.get("address") or None)
        except BacnetTransportError as exc:
            return _error("bacnet_error", str(exc), 502)

    @app.post("/api/bacnet/read")
    @protect
    async def read_many(request):
        src = _bacnet(source)
        if isinstance(src, Response):
            return src
        body = request.json or {}
        dev = body.get("device")
        parsed = _int_arg(str(dev) if dev is not None else None, "device")
        if isinstance(parsed, Response):
            return parsed
        reads = body.get("reads")
        if reads is None and body.get("object") and body.get("property"):
            reads = [{"object": body["object"], "property": body["property"]}]
        if not isinstance(reads, list) or not reads:
            return _error("bad_request", "reads must be a non-empty list", 400)
        cap = _read_cap()
        if len(reads) > cap:
            return _error("bad_request", f"reads is limited to {cap} properties (EDGE_BACNET_READ_MAX)", 400)
        pairs: list[tuple[str, str]] = []
        for item in reads:
            if not isinstance(item, dict):
                return _error("bad_request", "each read needs object and property", 400)
            obj = str(item.get("object") or "").strip()
            prop = str(item.get("property") or "").strip()
            if not obj or not prop:
                return _error("bad_request", "each read needs object and property", 400)
            pairs.append((obj, prop))
        try:
            rows = await src.read_properties(parsed, pairs, address=body.get("address") or None)
        except BacnetTransportError as exc:
            return _error("bacnet_error", str(exc), 502)
        return {"device": parsed, "results": rows}

    async def _mutate(request, *, release: bool):
        src = _bacnet(source)
        if isinstance(src, Response):
            return src
        username = _user(request)
        if _role(username) != "admin":
            return _error("forbidden", "BACnet writes require an admin role", 403)
        body = request.json or {}
        dev = _int_arg(str(body.get("device")) if body.get("device") is not None else None, "device")
        if isinstance(dev, Response):
            return dev
        obj = str(body.get("object") or "").strip()
        prop = str(body.get("property") or "present-value").strip()
        if not obj or not prop:
            return _error("bad_request", "object and property are required", 400)
        raw_pri = body.get("priority", 8)
        try:
            priority = int(raw_pri)
        except (TypeError, ValueError):
            return _error("bad_request", "priority must be an integer from 1 to 16", 400)
        try:
            if release:
                return await src.release_property(
                    dev, obj, property_identifier=prop, priority=priority, user=username,
                    address=body.get("address") or None,
                )
            if "value" not in body:
                return _error("bad_request", "value is required", 400)
            return await src.write_property(
                dev, obj, body.get("value"), property_identifier=prop, priority=priority,
                user=username, address=body.get("address") or None,
            )
        except PriorityRejected as exc:
            return _error("bad_request", str(exc), 400)
        except WriteDisabled as exc:
            return _error("forbidden", str(exc), 403)
        except BacnetTransportError as exc:
            return _error("bacnet_error", str(exc), 502)

    @app.post("/api/bacnet/write")
    @protect
    async def write(request):
        return await _mutate(request, release=False)

    @app.post("/api/bacnet/release")
    @protect
    async def release(request):
        return await _mutate(request, release=True)
