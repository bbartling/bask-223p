"""OpenAPI 3 description of every Microdot route on the edge."""
from __future__ import annotations

from edge_microdot import __version__

# auth: none | basic | bearer | session | bearer-or-session
# rate: none | login | export
_ROUTES: tuple[dict, ...] = (
    {"method": "get", "path": "/login", "tag": "auth", "auth": "none", "role": "public",
     "summary": "Dashboard login page.", "rate": "none"},
    {"method": "post", "path": "/login", "tag": "auth", "auth": "none", "role": "public",
     "summary": "Dashboard login. Sets the session cookie.", "rate": "login",
     "params": [{"name": "username", "in": "form"}, {"name": "password", "in": "form"}],
     "responses": {"302": "Session cookie set", "401": "Invalid credentials", "429": "Too many attempts"}},
    {"method": "get", "path": "/logout", "tag": "auth", "auth": "session", "role": "any",
     "summary": "Clear the dashboard session.", "rate": "none",
     "responses": {"302": "Redirect to /login"}},
    {"method": "post", "path": "/auth/token", "tag": "auth", "auth": "basic", "role": "any",
     "summary": "Exchange HTTP Basic credentials for an access and refresh token.", "rate": "none",
     "responses": {"200": "Token pair", "401": "Invalid credentials"}},
    {"method": "post", "path": "/auth/refresh", "tag": "auth", "auth": "none", "role": "public",
     "summary": "Rotate a refresh token.", "rate": "none",
     "params": [{"name": "refresh_token", "in": "body"}],
     "responses": {"200": "New token pair", "401": "Refresh token is not valid"}},
    {"method": "get", "path": "/auth/ws-token", "tag": "auth", "auth": "session", "role": "any",
     "summary": "Short-lived access token for the WebSocket.", "rate": "none"},
    {"method": "post", "path": "/api/auth/login", "tag": "auth", "auth": "none", "role": "public",
     "summary": "Open-FDD UI login. Access token in the body, refresh token in a cookie.", "rate": "login",
     "responses": {"200": "Access token and user role", "400": "Body must be username and password",
                   "401": "Invalid credentials", "404": "Auth is disabled on this host", "429": "Too many attempts"}},
    {"method": "post", "path": "/api/auth/refresh", "tag": "auth", "auth": "none", "role": "public",
     "summary": "Rotate the UI refresh cookie.", "rate": "none",
     "responses": {"200": "New access token", "401": "Refresh token is not valid", "404": "Auth is disabled"}},
    {"method": "post", "path": "/api/auth/logout", "tag": "auth", "auth": "none", "role": "public",
     "summary": "Revoke the UI refresh cookie.", "rate": "none",
     "responses": {"204": "Cookie cleared", "404": "Auth is disabled"}},
    {"method": "get", "path": "/", "tag": "pages", "auth": "session", "role": "any",
     "summary": "Dashboard HTML.", "rate": "none"},
    {"method": "get", "path": "/sql", "tag": "pages", "auth": "session", "role": "any",
     "summary": "Browser SQL page.", "rate": "none"},
    {"method": "get", "path": "/static/{path}", "tag": "pages", "auth": "none", "role": "public",
     "summary": "Static file under the application static directory.", "rate": "none",
     "params": [{"name": "path", "in": "path", "required": True}],
     "responses": {"200": "File", "404": "Not found"}},
    {"method": "get", "path": "/tls/allow", "tag": "health", "auth": "none", "role": "public",
     "summary": "Caddy on-demand TLS allow check.", "rate": "none",
     "params": [{"name": "domain", "in": "query"}],
     "responses": {"200": "Name is allowed", "403": "Name is not allowed"}},
    {"method": "get", "path": "/health", "tag": "health", "auth": "none", "role": "public",
     "summary": "Process status. No point values.", "rate": "none"},
    {"method": "get", "path": "/points", "tag": "telemetry", "auth": "bearer", "role": "viewer",
     "summary": "Configured and active point targets.", "rate": "none",
     "params": [{"name": "device", "in": "query"}, {"name": "object_type", "in": "query"}]},
    {"method": "get", "path": "/latest", "tag": "telemetry", "auth": "bearer", "role": "viewer",
     "summary": "Latest sample of each point in the hot cache.", "rate": "none"},
    {"method": "get", "path": "/history", "tag": "telemetry", "auth": "bearer", "role": "viewer",
     "summary": "Samples from the in-memory history store.", "rate": "none",
     "params": [{"name": "ref", "in": "query"}, {"name": "start", "in": "query"},
                {"name": "end", "in": "query"}, {"name": "limit", "in": "query"}]},
    {"method": "get", "path": "/rules", "tag": "rules", "auth": "bearer", "role": "viewer",
     "summary": "Latest local rule snapshot.", "rate": "none"},
    {"method": "get", "path": "/api/sql-rules", "tag": "rules", "auth": "bearer", "role": "viewer",
     "summary": "SQL rule files and the DuckDB-WASM bundle URLs.", "rate": "none"},
    {"method": "get", "path": "/api/export/bundle", "tag": "export", "auth": "bearer", "role": "viewer",
     "summary": "ZIP of Parquet history tables.", "rate": "none",
     "params": [{"name": "hours", "in": "query"}],
     "responses": {"200": "application/zip", "400": "hours is invalid", "401": "Authentication required"}},
    {"method": "post", "path": "/poll/once", "tag": "telemetry", "auth": "bearer", "role": "viewer",
     "summary": "Poll the configured source once.", "rate": "none"},
    {"method": "post", "path": "/sparql", "tag": "model", "auth": "bearer", "role": "viewer",
     "summary": "Read-only SPARQL when the Oxigraph store is enabled.", "rate": "none",
     "responses": {"200": "Query result", "400": "Query rejected", "401": "Authentication required",
                   "503": "SPARQL disabled"}},
    {"method": "get", "path": "/sparql/example", "tag": "model", "auth": "bearer", "role": "viewer",
     "summary": "Example SPARQL query and an optional preview.", "rate": "none"},
    {"method": "get", "path": "/live", "tag": "websocket", "auth": "bearer-or-session", "role": "viewer",
     "summary": "WebSocket of live samples. The token is a header or the session cookie, never a query parameter.",
     "rate": "none",
     "responses": {"101": "Switching protocols", "401": "Authentication required", "403": "Origin is not this host"}},
    {"method": "get", "path": "/api/meta", "tag": "ui", "auth": "none", "role": "public",
     "summary": "Open-FDD UI contract version and capability flags.", "rate": "none"},
    {"method": "get", "path": "/api/equipment", "tag": "ui", "auth": "bearer", "role": "viewer",
     "summary": "Equipment page.", "rate": "none",
     "params": [{"name": "limit", "in": "query"}, {"name": "cursor", "in": "query"}]},
    {"method": "get", "path": "/api/points", "tag": "ui", "auth": "bearer", "role": "viewer",
     "summary": "UI point page. This is not the CSV share.", "rate": "none",
     "params": [{"name": "equipment", "in": "query"}, {"name": "limit", "in": "query"}, {"name": "cursor", "in": "query"}]},
    {"method": "get", "path": "/api/samples", "tag": "ui", "auth": "bearer", "role": "viewer",
     "summary": "Time series for one point.", "rate": "none",
     "params": [{"name": "point", "in": "query", "required": True}, {"name": "from", "in": "query", "required": True},
                {"name": "to", "in": "query", "required": True}, {"name": "limit", "in": "query"},
                {"name": "cursor", "in": "query"}],
     "responses": {"200": "Sample page", "400": "Missing range", "401": "Authentication required", "404": "Unknown point"}},
    {"method": "get", "path": "/api/results", "tag": "ui", "auth": "bearer", "role": "viewer",
     "summary": "Rule results overlapping a time range.", "rate": "none",
     "params": [{"name": "from", "in": "query", "required": True}, {"name": "to", "in": "query", "required": True},
                {"name": "equipment", "in": "query"}, {"name": "rule", "in": "query"},
                {"name": "limit", "in": "query"}, {"name": "cursor", "in": "query"}]},
    {"method": "get", "path": "/api/rules", "tag": "ui", "auth": "bearer", "role": "viewer",
     "summary": "Rule catalog for the UI.", "rate": "none",
     "params": [{"name": "limit", "in": "query"}, {"name": "cursor", "in": "query"}]},
    {"method": "put", "path": "/api/rules/{rule_id}/confirm_min", "tag": "ui", "auth": "bearer", "role": "operator",
     "summary": "Tune confirm_min. Requires EDGE_RULES_WRITE=1, which is off by default.", "rate": "none",
     "params": [{"name": "rule_id", "in": "path", "required": True}, {"name": "confirm_min", "in": "body", "required": True}],
     "responses": {"200": "Updated rule", "400": "Body must be confirm_min", "401": "Authentication required",
                   "403": "Writes disabled or role is viewer", "404": "Unknown rule", "409": "Rule is not tunable"}},
    {"method": "get", "path": "/api/bacnet/whois", "tag": "bacnet", "auth": "bearer", "role": "viewer",
     "summary": "Range-limited Who-Is. low and high are both required.", "rate": "none",
     "params": [{"name": "low", "in": "query", "required": True}, {"name": "high", "in": "query", "required": True},
                {"name": "timeout", "in": "query"}, {"name": "address", "in": "query"}],
     "responses": {"200": "Devices that answered", "400": "low or high is missing", "401": "Authentication required",
                   "502": "BACnet transport error", "503": "BACnet driver is not active"}},
    {"method": "get", "path": "/api/bacnet/devices", "tag": "bacnet", "auth": "bearer", "role": "viewer",
     "summary": "Who-Is for each named device instance, plus one object-name read.", "rate": "none",
     "responses": {"200": "Named devices", "401": "Authentication required", "503": "BACnet driver is not active"}},
    {"method": "get", "path": "/api/bacnet/devices/{instance}", "tag": "bacnet", "auth": "bearer", "role": "viewer",
     "summary": "Device-object properties. This does not read the object list.", "rate": "none",
     "params": [{"name": "instance", "in": "path", "required": True}, {"name": "address", "in": "query"}],
     "responses": {"200": "Device info", "401": "Authentication required", "404": "Device did not answer",
                   "503": "BACnet driver is not active"}},
    {"method": "get", "path": "/api/bacnet/devices/{instance}/objects", "tag": "bacnet", "auth": "bearer", "role": "viewer",
     "summary": "Object catalog for one device. Does not read present-value and does not add points to the poll set.",
     "rate": "none",
     "params": [{"name": "instance", "in": "path", "required": True}, {"name": "address", "in": "query"}],
     "responses": {"200": "Object catalog", "401": "Authentication required", "502": "BACnet transport error",
                   "503": "BACnet driver is not active"}},
    {"method": "get", "path": "/api/bacnet/priority", "tag": "bacnet", "auth": "bearer", "role": "viewer",
     "summary": "Read the 16 priority slots and relinquish-default. This does not write.", "rate": "none",
     "params": [{"name": "device", "in": "query", "required": True}, {"name": "object", "in": "query", "required": True},
                {"name": "address", "in": "query"}],
     "responses": {"200": "Priority array", "400": "device or object is missing", "401": "Authentication required",
                   "502": "BACnet transport error", "503": "BACnet driver is not active"}},
    {"method": "post", "path": "/api/bacnet/read", "tag": "bacnet", "auth": "bearer", "role": "viewer",
     "summary": "ReadProperty or ReadPropertyMultiple for one device. Capped by EDGE_BACNET_READ_MAX (default 50).",
     "rate": "none",
     "params": [{"name": "device", "in": "body", "required": True}, {"name": "reads", "in": "body", "required": True}],
     "responses": {"200": "Property results", "400": "reads is empty or over the cap", "401": "Authentication required",
                   "502": "BACnet transport error", "503": "BACnet driver is not active"}},
    {"method": "post", "path": "/api/bacnet/write", "tag": "bacnet", "auth": "bearer", "role": "admin",
     "summary": "WriteProperty. Off unless EDGE_BACNET_WRITES_ENABLED=1, and only for an admin role.", "rate": "none",
     "params": [{"name": "device", "in": "body", "required": True}, {"name": "object", "in": "body", "required": True},
                {"name": "value", "in": "body", "required": True}, {"name": "property", "in": "body"},
                {"name": "priority", "in": "body"}],
     "responses": {"200": "Write accepted", "400": "Body or priority is invalid", "401": "Authentication required",
                   "403": "Writes are off or the role is not admin", "502": "BACnet transport error",
                   "503": "BACnet driver is not active"}},
    {"method": "post", "path": "/api/bacnet/release", "tag": "bacnet", "auth": "bearer", "role": "admin",
     "summary": "NULL write at a priority. Same gate as WriteProperty.", "rate": "none",
     "params": [{"name": "device", "in": "body", "required": True}, {"name": "object", "in": "body", "required": True},
                {"name": "priority", "in": "body"}, {"name": "property", "in": "body"}],
     "responses": {"200": "Released", "400": "Body or priority is invalid", "401": "Authentication required",
                   "403": "Writes are off or the role is not admin", "502": "BACnet transport error",
                   "503": "BACnet driver is not active"}},
    {"method": "get", "path": "/api/modbus/points", "tag": "modbus", "auth": "bearer", "role": "viewer",
     "summary": "Configured Modbus points. Does not read a register.", "rate": "none",
     "responses": {"200": "Point list", "401": "Authentication required", "503": "Modbus driver is not active"}},
    {"method": "post", "path": "/api/modbus/read", "tag": "modbus", "auth": "bearer", "role": "viewer",
     "summary": "Read configured holding or input registers. There is no Modbus write route.", "rate": "none",
     "params": [{"name": "names", "in": "body"}],
     "responses": {"200": "Samples", "400": "names is not a list", "401": "Authentication required",
                   "503": "Modbus driver is not active"}},
    {"method": "get", "path": "/api/model.ttl", "tag": "share", "auth": "bearer", "role": "viewer",
     "summary": "223P model as Turtle. Public only when EDGE_PUBLIC_MODEL=1.", "rate": "export",
     "responses": {"200": "text/turtle", "304": "Not modified", "401": "Authentication required", "429": "Rate limited"}},
    {"method": "get", "path": "/api/model", "tag": "share", "auth": "bearer", "role": "viewer",
     "summary": "223P model negotiated as Turtle, JSON-LD, or N-Triples.", "rate": "export",
     "params": [{"name": "Accept", "in": "header", "required": True}],
     "responses": {"200": "Negotiated RDF", "304": "Not modified", "401": "Authentication required",
                   "406": "Accept is not a supported RDF media type", "429": "Rate limited"}},
    {"method": "get", "path": "/api/points.csv", "tag": "share", "auth": "bearer", "role": "viewer",
     "summary": "Configured points only, with role, units, and bacnet or modbus source.", "rate": "export",
     "responses": {"200": "text/csv", "304": "Not modified", "401": "Authentication required", "429": "Rate limited"}},
    {"method": "get", "path": "/api/config/export", "tag": "share", "auth": "bearer", "role": "viewer",
     "summary": "Allowlisted configuration as JSON or YAML. Secrets are omitted.", "rate": "export",
     "params": [{"name": "format", "in": "query"}],
     "responses": {"200": "JSON or YAML", "304": "Not modified", "401": "Authentication required",
                   "406": "format is not json or yaml", "429": "Rate limited"}},
    {"method": "get", "path": "/api/export.zip", "tag": "share", "auth": "bearer", "role": "viewer",
     "summary": "model.ttl, points.csv, config.json, and manifest.json.", "rate": "export",
     "responses": {"200": "application/zip", "304": "Not modified", "401": "Authentication required", "429": "Rate limited"}},
    {"method": "get", "path": "/api/openapi.json", "tag": "meta", "auth": "bearer", "role": "viewer",
     "summary": "This document.", "rate": "none",
     "responses": {"200": "OpenAPI JSON", "401": "Authentication required"}},
)


def _security(auth: str) -> list[dict[str, list]]:
    if auth == "basic":
        return [{"basicAuth": []}]
    if auth in {"bearer", "bearer-or-session"}:
        return [{"bearerAuth": []}]
    return []


def _parameters(route: dict) -> list[dict]:
    params = []
    for item in route.get("params") or []:
        location = item["in"]
        if location == "form":
            continue
        if location == "body":
            continue
        schema: dict = {"type": "string"}
        entry = {
            "name": item["name"],
            "in": "header" if location == "header" else location,
            "required": bool(item.get("required", location == "path")),
            "schema": schema,
        }
        params.append(entry)
    return params


def _responses(route: dict) -> dict:
    stated = route.get("responses")
    if not stated:
        stated = {"200": "Success"}
        if route["auth"] not in {"none"}:
            stated["401"] = "Authentication required"
    out = {}
    for code, text in stated.items():
        out[str(code)] = {"description": text}
    return out


def build_openapi() -> dict:
    paths: dict[str, dict] = {}
    for route in _ROUTES:
        operation = {
            "tags": [route["tag"]],
            "summary": route["summary"],
            "operationId": f"{route['method']}_{route['path'].strip('/').replace('/', '_').replace('{', '').replace('}', '') or 'root'}",
            "security": _security(route["auth"]),
            "responses": _responses(route),
            "x-role": route["role"],
            "x-rate-limit": route["rate"],
            "x-auth": route["auth"],
        }
        if route["auth"] == "none":
            operation["security"] = []
        params = _parameters(route)
        if params:
            operation["parameters"] = params
        paths.setdefault(route["path"], {})[route["method"]] = operation
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Edge HTTP API",
            "version": __version__,
            "description": (
                "HTTP API for the edge service. "
                "BACnet WriteProperty and release stay off unless EDGE_BACNET_WRITES_ENABLED=1 "
                "and the caller is admin. Modbus has no write route. "
                "EDGE_RULES_WRITE, EDGE_PUBLIC_MODEL, EDGE_PUBLISH_ENABLED, and "
                "EDGE_BACNET_WRITES_ENABLED are off by default. "
                "Site models and point lists may be sensitive."
            ),
        },
        "servers": [{"url": "/"}],
        "tags": [
            {"name": "auth"},
            {"name": "pages"},
            {"name": "health"},
            {"name": "telemetry"},
            {"name": "rules"},
            {"name": "model"},
            {"name": "ui"},
            {"name": "bacnet"},
            {"name": "modbus"},
            {"name": "share"},
            {"name": "export"},
            {"name": "websocket"},
            {"name": "meta"},
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
                "basicAuth": {"type": "http", "scheme": "basic"},
            }
        },
    }
