"""Storage, auth, pull, and push against a fake edge."""
from __future__ import annotations

import io
import json
import logging
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from central.api import CentralServer
from central.config import ConfigError, load_config
from central.bind import resolve_interface
from central.fetch import FetchError, pull_export
from central.service import Collector, _zip_model
from central.store import list_snapshots

TOKEN = "central-token-canary"
EDGE_TOKEN = "edge-token-canary"
TTL = b"""
@prefix s223: <http://data.ashrae.org/standard223#> .
@prefix bacnet: <http://data.ashrae.org/bacnet/2020#> .
@prefix ref: <https://brickschema.org/schema/Brick/ref#> .
<https://example.org/bench/point/sa> a s223:Property ;
    s223:hasExternalReference <https://example.org/bench/bacnet-ref/sa> .
<https://example.org/bench/bacnet-ref/sa> a s223:BACnetExternalReference ;
    bacnet:object-identifier "analog-input,1" ;
    bacnet:device-identifier "device,5007" ;
    bacnet:object-name "SA-T" ;
    ref:BACnetURI "bacnet://5007/analog-input,1" .
"""


def _zip(ttl: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("model.ttl", ttl)
    return buf.getvalue()


class _EdgeHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.hits += 1
        if self.path != "/api/export.zip":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != f"Bearer {EDGE_TOKEN}":
            body = b"unauthorized"
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.server.hits == 1:
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.server.bundle
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _serve_edge(bundle: bytes):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EdgeHandler)
    server.hits = 0
    server.bundle = bundle
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _config(tmp: Path, base_url: str, keep: int = 2) -> Path:
    path = tmp / "central.yaml"
    path.write_text(
        f"""
interface: 127.0.0.1
port: 8780
data_dir: {tmp / "data"}
keep: {keep}
interval_s: 3600
token_env: CENTRAL_TOKEN
sparql: false
edges:
  - name: pi-bacnet
    site: bench
    base_url: {base_url}
    token_env: EDGE_PI_BACNET_TOKEN
    tls_verify: false
""",
        encoding="utf-8",
    )
    return path


def _start(collector: Collector) -> CentralServer:
    server = CentralServer(("127.0.0.1", 0), collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _request(server: CentralServer, method: str, path: str, token: str | None, body: bytes | None = None, headers=None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if body is not None:
        hdrs["Content-Length"] = str(len(body))
    request = Request(url, data=body, headers=hdrs, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_config_rejects_a_token_value(tmp_path: Path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
interface: 127.0.0.1
edges:
  - name: pi-bacnet
    site: bench
    base_url: https://192.0.2.11
    token_env: EDGE_TOKEN
    token: should-not-be-here
""",
        encoding="utf-8",
    )
    try:
        load_config(path)
    except ConfigError as exc:
        assert "token" in str(exc)
    else:
        raise AssertionError("token value was accepted")


def test_bind_refuses_every_interface():
    try:
        resolve_interface("0.0.0.0", allow_any=False)
    except ValueError as exc:
        assert "Ethernet" in str(exc)
    else:
        raise AssertionError("0.0.0.0 was accepted")
    assert resolve_interface("127.0.0.1") == "127.0.0.1"


def test_pull_stores_per_edge_and_requires_a_token(tmp_path: Path, caplog):
    edge = _serve_edge(_zip(TTL))
    try:
        cfg = load_config(_config(tmp_path, f"http://127.0.0.1:{edge.server_address[1]}"))
        environ = {
            "CENTRAL_TOKEN": TOKEN,
            "EDGE_PI_BACNET_TOKEN": EDGE_TOKEN,
        }
        collector = Collector(cfg, environ, sleep=lambda _seconds: None)
        caplog.set_level(logging.INFO)
        result = collector.pull_all()
        assert result["pulled"][0]["unchanged"] is False
        assert TOKEN not in caplog.text
        assert EDGE_TOKEN not in caplog.text
        folders = list_snapshots(cfg.data_dir, "bench", "pi-bacnet")
        assert len(folders) == 1
        assert (folders[0] / "model.ttl").is_file()
        assert folders[0].parent == cfg.data_dir / "bench" / "pi-bacnet"
        again = collector.pull_edge(cfg.edges[0])
        assert again["unchanged"] is True
        assert len(list_snapshots(cfg.data_dir, "bench", "pi-bacnet")) == 1

        changed = TTL.replace(b"SA-T", b"SA-T2")
        edge.bundle = _zip(changed)
        edge.hits = 1
        collector.cfg.keep = 2
        collector.pull_edge(cfg.edges[0])
        edge.bundle = _zip(changed.replace(b"SA-T2", b"SA-T3"))
        edge.hits = 1
        collector.pull_edge(cfg.edges[0])
        edge.bundle = _zip(changed.replace(b"SA-T2", b"SA-T4"))
        edge.hits = 1
        collector.pull_edge(cfg.edges[0])
        kept = list_snapshots(cfg.data_dir, "bench", "pi-bacnet")
        assert len(kept) == 2

        server = _start(collector)
        status, _headers, body = _request(server, "GET", "/api/edges", None)
        assert status == 401
        status, headers, body = _request(server, "GET", "/api/edges", TOKEN)
        assert status == 200
        payload = json.loads(body)
        assert payload["edges"][0]["site"] == "bench"
        assert payload["edges"][0]["last_seen"]
        assert TOKEN not in body.decode()
        status, _headers, spec = _request(server, "GET", "/api/openapi.json", TOKEN)
        assert status == 200
        document = json.loads(spec)
        assert "/api/sparql" not in document["paths"]
        assert "/api/reconcile" in document["paths"]
        status, _headers, _body = _request(server, "POST", "/api/sparql", TOKEN, b"SELECT * WHERE { ?s ?p ?o }")
        assert status == 404
        status, _headers, model = _request(server, "GET", "/api/edges/bench/pi-bacnet/model.ttl", TOKEN)
        assert status == 200
        assert b"bacnet://5007/analog-input,1" in model
        status, _headers, report = _request(server, "GET", "/api/validation", TOKEN)
        assert status == 200
        assert "references" in json.loads(report)
        server.shutdown()
    finally:
        edge.shutdown()


def test_push_is_authenticated_and_idempotent(tmp_path: Path):
    cfg = load_config(_config(tmp_path, "http://127.0.0.1:9"))
    environ = {"CENTRAL_TOKEN": TOKEN, "EDGE_PI_BACNET_TOKEN": EDGE_TOKEN}
    collector = Collector(cfg, environ)
    server = _start(collector)
    try:
        status, _headers, _body = _request(
            server, "POST", "/api/edges/bench/pi-bacnet/export", None, TTL, {"Content-Type": "text/turtle"}
        )
        assert status == 401
        status, headers, body = _request(
            server,
            "POST",
            "/api/edges/bench/pi-bacnet/export",
            EDGE_TOKEN,
            TTL,
            {"Content-Type": "text/turtle", "Idempotency-Key": "same"},
        )
        assert status == 201
        first = json.loads(body)["snapshot"]
        status, headers, body = _request(
            server,
            "POST",
            "/api/edges/bench/pi-bacnet/export",
            TOKEN,
            TTL,
            {"Content-Type": "text/turtle", "Idempotency-Key": "same"},
        )
        assert status == 200
        assert headers["Idempotent"] == "true"
        assert json.loads(body)["snapshot"] == first
        other = TTL.replace(b"SA-T", b"OTHER")
        status, _headers, _body = _request(
            server,
            "POST",
            "/api/edges/bench/pi-bacnet/export",
            TOKEN,
            other,
            {"Content-Type": "text/turtle", "Idempotency-Key": "same"},
        )
        assert status == 409
        status, _headers, trig = _request(server, "GET", "/api/model.trig", TOKEN)
        assert status == 200
        assert b"urn:central:graph:bench:pi-bacnet" in trig
        status, _headers, _body = _request(server, "POST", "/api/pull", "wrong-token", b"")
        assert status == 401
    finally:
        server.shutdown()


def test_pull_refuses_an_oversized_body():
    edge = _serve_edge(_zip(TTL))
    try:
        with pytest.raises(FetchError, match="max_upload_bytes"):
            pull_export(
                f"http://127.0.0.1:{edge.server_address[1]}",
                EDGE_TOKEN,
                attempts=3,
                backoff_s=0,
                max_bytes=8,
                verify=False,
            )
    finally:
        edge.shutdown()


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.auth = self.headers.get("Authorization")
        self.send_response(302)
        self.send_header("Location", self.server.location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        return


class _SinkHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.seen = self.headers.get("Authorization")
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, fmt, *args):
        return


def test_pull_does_not_follow_a_redirect():
    sink = ThreadingHTTPServer(("127.0.0.1", 0), _SinkHandler)
    sink.seen = None
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    edge = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    edge.location = f"http://127.0.0.1:{sink.server_address[1]}/stolen"
    edge.auth = None
    threading.Thread(target=edge.serve_forever, daemon=True).start()
    try:
        with pytest.raises(FetchError, match="HTTP 302"):
            pull_export(
                f"http://127.0.0.1:{edge.server_address[1]}",
                EDGE_TOKEN,
                attempts=1,
                verify=False,
            )
        assert sink.seen is None
        assert edge.auth == f"Bearer {EDGE_TOKEN}"
    finally:
        edge.shutdown()
        sink.shutdown()


def test_zip_model_rejects_traversal_and_expansion():
    slipped = io.BytesIO()
    with zipfile.ZipFile(slipped, "w") as archive:
        archive.writestr("model.ttl", b"@prefix s223: <http://data.ashrae.org/standard223#> .\n")
        archive.writestr("../secret", b"nope")
    with pytest.raises(FetchError, match="one model.ttl"):
        _zip_model(slipped.getvalue(), 1000)

    huge = io.BytesIO()
    with zipfile.ZipFile(huge, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("model.ttl", b"x" * 80)
    with pytest.raises(FetchError, match="max_upload_bytes"):
        _zip_model(huge.getvalue(), 10)
