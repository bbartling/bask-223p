"""Authenticated REST API. SPARQL is off unless the config flag is set."""
from __future__ import annotations

import hmac
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .openapi import build_openapi
from .service import Collector, IdempotencyConflict, env_token
from .upload import UploadError

LOG = logging.getLogger("central.api")

_EDGE_MODEL = re.compile(r"^/api/edges/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})/model\.ttl$")
_EDGE_EXPORT = re.compile(r"^/api/edges/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})/export$")
_PULL_ONE = re.compile(r"^/api/pull/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})$")


def _bearer_ok(header: str | None, token: str) -> bool:
    if not header:
        return False
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    got = rest.strip().encode("utf-8")
    expected = token.encode("utf-8")
    if len(got) != len(expected):
        return False
    return hmac.compare_digest(got, expected)


class CentralHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "CentralServer"

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200, extra: dict[str, str] | None = None) -> None:
        body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _error(self, status: int, code: str, message: str, **headers: str) -> None:
        self._json({"error": {"code": code, "message": message}}, status, extra=headers or None)

    def _collector(self) -> Collector:
        return self.server.collector

    def _authorized(self, site: str | None = None, edge: str | None = None) -> bool:
        header = self.headers.get("Authorization")
        collector = self._collector()
        try:
            if _bearer_ok(header, env_token(collector.cfg.token_env, collector.environ)):
                return True
        except LookupError:
            return False
        if site and edge:
            configured = collector.edge_by_key(site, edge)
            if configured is None:
                return False
            try:
                return _bearer_ok(header, env_token(configured.token_env, collector.environ))
            except LookupError:
                return False
        return False

    def _require(self, site: str | None = None, edge: str | None = None) -> bool:
        if self._authorized(site, edge):
            return True
        self._error(401, "unauthorized", "Authentication required", **{"WWW-Authenticate": "Bearer"})
        return False

    def _body(self) -> bytes | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            self._error(411, "length_required", "Content-Length is required")
            return None
        try:
            length = int(raw)
        except ValueError:
            self._error(400, "bad_request", "Content-Length is not an integer")
            return None
        if length < 0:
            self._error(400, "bad_request", "Content-Length is not an integer")
            return None
        if length > self._collector().cfg.max_upload_bytes:
            self._error(413, "too_large", "body exceeds max_upload_bytes")
            return None
        return self.rfile.read(length)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if not self._require():
            return
        collector = self._collector()
        if path == "/api/openapi.json":
            self._json(build_openapi(sparql=collector.cfg.sparql))
            return
        if path == "/api/edges":
            self._json(collector.list_edges())
            return
        if path in {"/api/model", "/api/model.ttl", "/api/model.trig", "/api/model.jsonld", "/api/model.nq"}:
            self._model(path)
            return
        if path == "/api/reconcile":
            self._json(collector.reconcile_report)
            return
        if path == "/api/validation":
            self._json(collector.validation_report)
            return
        match = _EDGE_MODEL.match(path)
        if match:
            payload = collector.model_bytes(match.group(1), match.group(2))
            if payload is None:
                self._error(404, "not_found", "No export for that edge")
                return
            self._send(200, payload, "text/turtle; charset=utf-8")
            return
        if path == "/api/sparql":
            self._error(404, "sparql_disabled", "SPARQL is off. Set sparql: true to enable it")
            return
        self._error(404, "not_found", "No such route")

    def _model(self, path: str) -> None:
        collector = self._collector()
        if collector.merged is None:
            self._error(404, "not_found", "No exports stored")
            return
        accept = self.headers.get("Accept") or ""
        kind = "turtle"
        if path.endswith(".trig"):
            kind = "trig"
        elif path.endswith(".jsonld"):
            kind = "jsonld"
        elif path.endswith(".nq"):
            kind = "nq"
        elif "application/trig" in accept:
            kind = "trig"
        elif "application/n-quads" in accept:
            kind = "nq"
        elif "application/ld+json" in accept:
            kind = "jsonld"
        if kind == "trig":
            self._send(200, collector.trig(), "application/trig; charset=utf-8")
        elif kind == "nq":
            self._send(200, collector.nquads(), "application/n-quads; charset=utf-8")
        elif kind == "jsonld":
            from .merge import jsonld_document

            body = (json.dumps(jsonld_document(collector.merged), indent=2) + "\n").encode("utf-8")
            self._send(200, body, "application/ld+json; charset=utf-8")
        else:
            self._send(200, collector.turtle(), "text/turtle; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        collector = self._collector()
        if path == "/api/sparql":
            if not self._require():
                return
            if not collector.cfg.sparql:
                self._error(404, "sparql_disabled", "SPARQL is off. Set sparql: true to enable it")
                return
            body = self._body()
            if body is None:
                return
            try:
                result = collector.query(body.decode("utf-8"))
            except ValueError as exc:
                self._error(400, "bad_request", str(exc))
                return
            except Exception as exc:
                self._error(400, "bad_request", str(exc)[:300])
                return
            self._json(_sparql_json(result))
            return
        export = _EDGE_EXPORT.match(path)
        if export:
            site, edge = export.group(1), export.group(2)
            if not self._require(site, edge):
                return
            body = self._body()
            if body is None:
                return
            key = self.headers.get("Idempotency-Key")
            try:
                saved = collector.accept_push(site, edge, body, self.headers.get("Content-Type") or "", key)
            except KeyError:
                self._error(404, "not_found", "Edge is not in the config")
                return
            except UploadError as exc:
                self._error(400, "bad_request", str(exc))
                return
            except IdempotencyConflict:
                self._error(409, "idempotency_conflict", "Idempotency-Key was already used for a different body")
                return
            status = 200 if saved["unchanged"] else 201
            extra = {"Idempotent": "true"} if saved["unchanged"] else None
            self._json(saved, status, extra)
            return
        if path == "/api/pull":
            if not self._require():
                return
            self._json(collector.pull_all())
            return
        one = _PULL_ONE.match(path)
        if one:
            if not self._require():
                return
            edge = collector.edge_by_key(one.group(1), one.group(2))
            if edge is None:
                self._error(404, "not_found", "Edge is not in the config")
                return
            try:
                row = collector.pull_edge(edge)
            except Exception as exc:
                self._error(502, "pull_failed", str(exc)[:300])
                return
            collector.rebuild()
            self._json({"pulled": [row]})
            return
        if not self._require():
            return
        self._error(404, "not_found", "No such route")


def _sparql_json(result) -> dict:
    kind = getattr(result, "type", "")
    if kind == "ASK":
        return {"head": {}, "boolean": bool(result.askAnswer)}
    if kind == "SELECT":
        names = [str(var) for var in (result.vars or [])]
        rows = []
        for binding in result:
            rows.append({str(key): str(value) for key, value in binding.items()})
        return {"head": {"vars": names}, "results": {"bindings": rows}}
    return {"head": {}, "boolean": True}


class CentralServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], collector: Collector):
        self.collector = collector
        super().__init__(address, CentralHandler)


def serve(collector: Collector) -> None:
    host = collector.cfg.host or "127.0.0.1"
    server = CentralServer((host, collector.cfg.port), collector)
    LOG.info("listening on %s:%s", host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        server.server_close()
