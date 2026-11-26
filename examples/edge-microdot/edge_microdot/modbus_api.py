"""Read-only Modbus routes. Configured points only, same bearer token as /api."""
from __future__ import annotations

from typing import Any

from microdot import Response

from edge_microdot.security import api_auth


def _error(code: str, message: str, status: int) -> Response:
    return Response({"error": {"code": code, "message": message}}, status_code=status)


def attach_modbus_api(app, *, source, auth) -> None:
    protect = api_auth(auth)

    def _source() -> Any | Response:
        if source is None or not hasattr(source, "read_points") or not hasattr(source, "points"):
            return _error("modbus_inactive", "Modbus driver is not active", 503)
        return source

    @app.get("/api/modbus/points")
    @protect
    async def points(request):
        src = _source()
        if isinstance(src, Response):
            return src
        rows = [point.to_dict() for point in src.points]
        return {"count": len(rows), "points": rows}

    @app.post("/api/modbus/read")
    @protect
    async def read(request):
        src = _source()
        if isinstance(src, Response):
            return src
        body = request.json or {}
        names = body.get("names")
        if names is not None and not isinstance(names, list):
            return _error("bad_request", "names must be a list of configured point names", 400)
        samples = await src.read_points(names)
        return {"count": len(samples), "samples": [sample.to_dict() for sample in samples]}
