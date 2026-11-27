"""Share API: auth, content types, redaction, caching, and opt-in publish."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from datetime import datetime, timezone

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from edge_microdot.cache import HotCache
from edge_microdot.config import LoadedConfig
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointTarget
from edge_microdot.publish import PublishDisabled, publish_cli, publish_once
from edge_microdot.security import load_auth_from_env
from edge_microdot.share import (
    build_export_zip,
    redact_config,
    reset_export_limits,
    role_file_status,
    serialize_model,
)
from edge_microdot.sources.fake import FakePollSource
from edge_microdot.sparql_kb import SparqlKb

CANARY = "CANARY-SECRET-9f3e2a7c"
TOKEN = "publish-token-canary-9f3e2a7c"


def _graph() -> Graph:
    graph = Graph()
    graph.add(
        (
            URIRef("https://example.org/site/point/1"),
            RDF.type,
            URIRef("http://data.ashrae.org/standard223#Property"),
        )
    )
    graph.add((URIRef("https://example.org/site/point/1"), URIRef("https://example.org/ns#label"), Literal("zone")))
    return graph


def _targets() -> tuple[PointTarget, PointTarget, PointTarget]:
    bacnet = PointTarget(
        site="demo",
        device="10001",
        object_id="analog-input,1",
        units="degC",
        label="zone",
        ref="bacnet://10001/analog-input,1",
        extra={"role": "zone_air_temperature", "password": CANARY, "api_key": CANARY + "-key"},
    )
    modbus = PointTarget(
        site="demo",
        device="1",
        object_id="holding,40001",
        units="KiloW-HR",
        label="meter",
        ref="modbus://1/40001",
        extra={"protocol": "modbus", "role": "supply_fan_command"},
    )
    discovered = PointTarget(
        site="demo",
        device="10001",
        object_id="analog-input,99",
        label="DISCOVERED-ONLY-POINT",
        ref="bacnet://10001/analog-input,99",
    )
    return bacnet, modbus, discovered


@pytest.fixture
def share_app(tmp_path, monkeypatch):
    reset_export_limits()
    monkeypatch.delenv("EDGE_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("EDGE_PUBLIC_MODEL", raising=False)
    monkeypatch.delenv("EDGE_PUBLISH_ENABLED", raising=False)
    monkeypatch.setenv("EDGE_EXPORT_RATE_LIMIT", "100")
    monkeypatch.setenv("NIAGARA_PASS", CANARY + "-niagara")
    monkeypatch.setenv("EDGE_PUBLISH_TOKEN", TOKEN)
    monkeypatch.setenv("EDGE_SECRET_KEY", "unit-test-secret-key-32-bytes-minimum")
    bacnet, modbus, discovered = _targets()
    graph = _graph()
    cfg = LoadedConfig(site="demo", source="bacnet", local_address="192.0.2.10/24", targets=[bacnet, modbus])
    cfg.station = "https://user:" + CANARY + "-station@station.example"
    cfg.graph = graph
    history = HistoryStore(tmp_path / "h.duckdb")
    app = __import__("edge_microdot.app", fromlist=["create_app"]).create_app(
        source=FakePollSource([bacnet, modbus, discovered]),
        source_kind="bacnet",
        targets=[bacnet, modbus],
        cfg=cfg,
        hot=HotCache("dict"),
        history=history,
        fdd=FddEngine(history=history),
        sparql=SparqlKb(graph),
        auth=load_auth_from_env(),
        site="demo",
        poll_interval_s=3600,
        flush_interval_s=3600,
        fdd_interval_s=3600,
    )
    yield app
    history.close()


def _header(response, name: str) -> str:
    for key, value in response.headers.items():
        if str(key).lower() == name.lower():
            if isinstance(value, list):
                return str(value[0])
            return str(value)
    raise AssertionError(f"missing header {name}")


async def _token(client) -> dict[str, str]:
    import base64

    resp = await client.post(
        "/auth/token",
        headers={"Authorization": "Basic " + base64.b64encode(b"operator:operator-pass").decode("ascii")},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json['access_token']}"}


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_redaction_leaks_nothing():
    """Allowlist copy drops every non-listed key, including innocent names."""
    hostile = {
        "site": "demo",
        "password": CANARY + "-password",
        "secret": CANARY + "-secret",
        "token": CANARY + "-token",
        "api_key": CANARY + "-apikey",
        "private_key": CANARY + "-private",
        "notes": CANARY + "-notes",
        "comment": {"password": CANARY + "-comment", "site": "should-not-keep-nested"},
        "authorization": CANARY + "-authz",
        "users": "operator:" + CANARY + "-users",
        "public_key": CANARY + "-pubkey",
        "token_endpoint": CANARY + "-endpoint",
        "niagara_pass": CANARY + "-pass",
        "points": [
            {
                "site": "demo",
                "label": "zone",
                "role": "zone_air_temperature",
                "units": "degC",
                "source": "bacnet",
                "password": CANARY + "-point",
                "extra": {"token": CANARY + "-extra"},
                "notes": CANARY + "-point-notes",
            }
        ],
    }
    cleaned = redact_config(hostile)
    blob = json.dumps(cleaned) + "\n" + __import__("yaml").safe_dump(cleaned)
    assert CANARY not in blob
    assert "should-not-keep-nested" not in blob
    assert cleaned["site"] == "demo"
    assert cleaned["points"][0]["role"] == "zone_air_temperature"
    assert "password" not in cleaned["points"][0]
    assert set(cleaned) <= {"site", "points"}


def test_share_requires_auth_and_serves_model_points_and_config(share_app):
    from microdot.test_client import TestClient

    async def _go():
        client = TestClient(share_app)
        anonymous = await client.get("/api/model.ttl")
        assert anonymous.status_code == 401
        assert anonymous.json["error"]["code"] == "unauthorized"
        headers = await _token(client)

        turtle = await client.get("/api/model.ttl", headers=headers)
        assert turtle.status_code == 200
        assert _header(turtle, "Content-Type").startswith("text/turtle")
        assert _header(turtle, "ETag")
        assert _header(turtle, "Last-Modified")
        parsed = Graph().parse(data=turtle.text, format="turtle")
        assert len(parsed) == 2

        negotiated = await client.get("/api/model", headers={**headers, "Accept": "text/turtle"})
        assert negotiated.status_code == 200
        assert _header(negotiated, "Content-Type").startswith("text/turtle")
        Graph().parse(data=negotiated.text, format="turtle")

        jsonld = await client.get("/api/model", headers={**headers, "Accept": "application/ld+json"})
        assert jsonld.status_code == 200
        assert _header(jsonld, "Content-Type").startswith("application/ld+json")
        Graph().parse(data=jsonld.text, format="json-ld")

        ntriples = await client.get(
            "/api/model",
            headers={**headers, "Accept": "application/n-triples;q=0.2, text/turtle;q=0.9"},
        )
        assert _header(ntriples, "Content-Type").startswith("text/turtle")
        only_nt = await client.get("/api/model", headers={**headers, "Accept": "application/n-triples"})
        assert only_nt.status_code == 200
        assert _header(only_nt, "Content-Type").startswith("application/n-triples")
        Graph().parse(data=only_nt.text, format="nt")

        rejected = await client.get("/api/model", headers={**headers, "Accept": "application/rdf+xml"})
        assert rejected.status_code == 406

        table = await client.get("/api/points.csv", headers=headers)
        assert table.status_code == 200
        assert _header(table, "Content-Type").startswith("text/csv")
        rows = table.text.strip().split("\n")
        assert rows[0].split(",")[:5] == ["site", "label", "role", "units", "source"]
        body = table.text
        assert "zone_air_temperature" in body
        assert "degC" in body
        assert "bacnet" in body
        assert "modbus" in body
        assert "supply_fan_command" in body
        assert "DISCOVERED-ONLY-POINT" not in body
        assert CANARY not in body

        exported = await client.get("/api/config/export", headers=headers)
        assert exported.status_code == 200
        assert _header(exported, "Content-Type").startswith("application/json")
        document = exported.json
        assert document["site"] == "demo"
        assert document["local_address"] == "192.0.2.10/24"
        assert CANARY not in exported.text
        assert TOKEN not in exported.text
        assert "unit-test-secret-key-32-bytes-minimum" not in exported.text

        yaml_resp = await client.get("/api/config/export?format=yaml", headers=headers)
        assert yaml_resp.status_code == 200
        assert _header(yaml_resp, "Content-Type").startswith("application/yaml")
        loaded = __import__("yaml").safe_load(yaml_resp.text)
        assert loaded["points"][0]["role"] == "zone_air_temperature"
        assert CANARY not in yaml_resp.text
        assert TOKEN not in yaml_resp.text

        bad = await client.get("/api/config/export?format=xml", headers=headers)
        assert bad.status_code == 406

        cached = await client.get("/api/model.ttl", headers={**headers, "If-None-Match": _header(turtle, "ETag")})
        assert cached.status_code == 304
        assert cached.body in {b"", None}

        bundle = await client.get("/api/export.zip", headers=headers)
        assert bundle.status_code == 200
        assert _header(bundle, "Content-Type").startswith("application/zip")
        assert CANARY not in bundle.body.decode("latin1")
        with zipfile.ZipFile(io.BytesIO(bundle.body)) as archive:
            names = set(archive.namelist())
            assert names == {"model.ttl", "points.csv", "config.json", "manifest.json"}
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["edge_version"]
            assert manifest["generated_at"].endswith("Z")
            assert manifest["role_file"]["name"] == "roles/openfdd-roles.json"
            for name, meta in manifest["files"].items():
                payload = archive.read(name)
                assert meta["sha256"] == hashlib.sha256(payload).hexdigest()
                assert meta["bytes"] == len(payload)
                assert CANARY not in payload.decode("utf-8")
            Graph().parse(data=archive.read("model.ttl"), format="turtle")

    _run(_go())


def test_public_model_is_opt_in_and_limited_to_turtle(share_app, monkeypatch):
    from microdot.test_client import TestClient

    async def _go():
        client = TestClient(share_app)
        monkeypatch.setenv("EDGE_PUBLIC_MODEL", "1")
        public = await client.get("/api/model.ttl")
        assert public.status_code == 200
        assert _header(public, "Content-Type").startswith("text/turtle")
        for path in ("/api/model", "/api/points.csv", "/api/config/export", "/api/export.zip", "/api/openapi.json"):
            hidden = await client.get(path, headers={"Accept": "text/turtle"})
            assert hidden.status_code == 401, path

    _run(_go())


def test_export_rate_limit(share_app, monkeypatch):
    from microdot.test_client import TestClient

    monkeypatch.setenv("EDGE_EXPORT_RATE_LIMIT", "2")
    monkeypatch.setenv("EDGE_EXPORT_RATE_WINDOW", "60")
    reset_export_limits()

    async def _go():
        client = TestClient(share_app)
        headers = await _token(client)
        assert (await client.get("/api/model.ttl", headers=headers)).status_code == 200
        assert (await client.get("/api/points.csv", headers=headers)).status_code == 200
        limited = await client.get("/api/export.zip", headers=headers)
        assert limited.status_code == 429
        assert limited.json["error"]["code"] == "rate_limited"
        assert _header(limited, "Retry-After")

    _run(_go())


def test_serialize_uses_oxigraph_when_the_store_exists():
    pyoxigraph = pytest.importorskip("pyoxigraph")
    graph = _graph()
    kb = SparqlKb(graph)
    assert kb.enabled
    seen: dict[str, object] = {}
    real = kb.store.dump

    class _Proxy:
        def dump(self, *args, **kwargs):
            seen["format"] = kwargs.get("format")
            seen["from_graph"] = kwargs.get("from_graph")
            return real(*args, **kwargs)

    payload = serialize_model(graph, _Proxy(), "nt")
    assert seen["format"] == pyoxigraph.RdfFormat.N_TRIPLES
    assert isinstance(seen["from_graph"], pyoxigraph.DefaultGraph)
    parsed = Graph().parse(data=payload, format="nt")
    assert len(parsed) == 2
    fallback = serialize_model(graph, None, "json-ld")
    assert len(Graph().parse(data=fallback, format="json-ld")) == 2


def test_manifest_role_file_hash(tmp_path, monkeypatch):
    missing = role_file_status({"EDGE_ROLES_FILE": str(tmp_path / "absent.json")})
    # An explicit missing override still searches the vendored paths.
    if not (tmp_path / "absent.json").exists():
        present_default = role_file_status({})
        if not present_default["present"]:
            assert missing["sha256"] is None or missing["present"] is True
    path = tmp_path / "openfdd-roles.json"
    payload = b'{"roles":["zone_air_temperature"]}\n'
    path.write_bytes(payload)
    monkeypatch.setenv("EDGE_ROLES_FILE", str(path))
    found = role_file_status()
    assert found["present"] is True
    assert found["sha256"] == hashlib.sha256(payload).hexdigest()
    assert found["name"] == "roles/openfdd-roles.json"
    blob = build_export_zip(
        graph=_graph(),
        store=None,
        cfg=LoadedConfig(site="demo"),
        targets=[],
        generated_at="2026-01-01T00:00:00Z",
    )
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["role_file"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["generated_at"] == "2026-01-01T00:00:00Z"
    assert manifest["edge_version"]


def test_publish_is_off_by_default_and_does_not_log_the_token(monkeypatch, caplog, capsys):
    monkeypatch.delenv("EDGE_PUBLISH_ENABLED", raising=False)
    assert publish_cli([]) == 2
    assert "sensitive" in capsys.readouterr().out.lower()

    monkeypatch.setenv("EDGE_PUBLISH_ENABLED", "1")
    monkeypatch.delenv("EDGE_PUBLISH_TOKEN", raising=False)
    assert publish_cli([]) == 1
    assert "EDGE_PUBLISH_TOKEN" in capsys.readouterr().out
    assert TOKEN not in capsys.readouterr().out

    class _Response:
        def __init__(self, status, payload=None):
            self.status_code = status
            self._payload = payload or {}

        def json(self):
            return self._payload

    class _Transport:
        def __init__(self):
            self.calls = []

        def request(self, method, url, headers=None, timeout=None, **kwargs):
            self.calls.append({"method": method, "url": url, "headers": headers or {}, **kwargs})
            if method == "GET" and url.endswith("/releases/tags/edge-share-demo-pi-1"):
                return _Response(404)
            if method == "POST" and url.endswith("/releases"):
                return _Response(
                    201,
                    {"upload_url": "https://uploads.example/assets{?name,label}", "assets": []},
                )
            if method == "POST" and url.startswith("https://uploads.example/"):
                return _Response(201, {"name": kwargs.get("params", {}).get("name")})
            raise AssertionError((method, url))

    transport = _Transport()
    caplog.set_level(logging.INFO)
    result = publish_once(
        cfg=LoadedConfig(site="demo"),
        targets=[],
        graph=_graph(),
        session=transport,
        environ={
            "EDGE_PUBLISH_ENABLED": "1",
            "EDGE_PUBLISH_TOKEN": TOKEN,
            "EDGE_PUBLISH_REPO": "example/edge-share",
            "EDGE_PUBLISH_KIND": "release",
            "EDGE_NAME": "pi-1",
        },
    )
    assert result == {
        "repo": "example/edge-share",
        "kind": "release",
        "ref": "edge-share-demo-pi-1",
        "site": "demo",
        "edge": "pi-1",
        "files": ["model.ttl", "export.zip"],
    }
    assert TOKEN not in json.dumps(result)
    assert TOKEN not in caplog.text
    names = []
    for call in transport.calls:
        assert TOKEN not in call["url"]
        assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
        if call["url"].startswith("https://uploads.example/"):
            names.append(call["params"]["name"])
    assert names == ["model.ttl", "export.zip"]

    with pytest.raises(PublishDisabled):
        publish_once(cfg=LoadedConfig(site="demo"), targets=[], environ={})


def test_publish_can_write_a_repository_branch():
    class _Response:
        def __init__(self, status, payload=None):
            self.status_code = status
            self._payload = payload or {}

        def json(self):
            return self._payload

    class _Transport:
        def __init__(self):
            self.puts = []

        def request(self, method, url, headers=None, timeout=None, **kwargs):
            if method == "GET":
                return _Response(404)
            if method == "PUT":
                assert kwargs["json"]["branch"] == "edge-share"
                assert "content" in kwargs["json"]
                self.puts.append(url)
                return _Response(201, {"content": {"path": url.rsplit("/", 1)[-1]}})
            raise AssertionError((method, url))

    transport = _Transport()
    result = publish_once(
        cfg=LoadedConfig(site="demo"),
        targets=[],
        session=transport,
        environ={
            "EDGE_PUBLISH_ENABLED": "1",
            "EDGE_PUBLISH_TOKEN": TOKEN,
            "EDGE_PUBLISH_REPO": "example/edge-share",
            "EDGE_PUBLISH_KIND": "repo",
            "EDGE_PUBLISH_BRANCH": "edge-share",
            "EDGE_NAME": "pi-1",
        },
    )
    assert result["kind"] == "repo"
    assert result["files"] == ["share/demo/pi-1/model.ttl", "share/demo/pi-1/export.zip"]
    assert all(url.endswith(path) for url, path in zip(transport.puts, result["files"]))
    assert TOKEN not in json.dumps(result)


def test_two_edges_publish_to_different_paths():
    class _Response:
        def __init__(self, status, payload=None):
            self.status_code = status
            self._payload = payload or {}

        def json(self):
            return self._payload

    class _Transport:
        def __init__(self):
            self.calls = []

        def request(self, method, url, headers=None, timeout=None, **kwargs):
            self.calls.append((method, url, kwargs))
            if method == "GET" and "/releases/tags/" in url:
                return _Response(404)
            if method == "POST" and url.endswith("/releases"):
                return _Response(201, {"upload_url": "https://uploads.example/assets{?name,label}", "assets": []})
            if method == "POST" and url.startswith("https://uploads.example/"):
                return _Response(201, {})
            if method == "GET":
                return _Response(404)
            if method == "PUT":
                return _Response(201, {})
            raise AssertionError((method, url))

    shared = {
        "EDGE_PUBLISH_ENABLED": "1",
        "EDGE_PUBLISH_TOKEN": TOKEN,
        "EDGE_PUBLISH_REPO": "example/edge-share",
        "EDGE_PUBLISH_TAG": "edge-share",
        "EDGE_PUBLISH_BRANCH": "edge-share",
    }
    release_refs = []
    repo_paths = []
    for edge in ("pi-bacnet", "pi-niagara"):
        transport = _Transport()
        publish_once(
            cfg=LoadedConfig(site="bench"),
            targets=[],
            graph=_graph(),
            session=transport,
            environ={**shared, "EDGE_PUBLISH_KIND": "release", "EDGE_SITE": "bench", "EDGE_NAME": edge},
        )
        tags = [url.rsplit("/", 1)[-1] for method, url, _ in transport.calls if "/releases/tags/" in url]
        release_refs.append(tags[0])
        transport = _Transport()
        result = publish_once(
            cfg=LoadedConfig(site="bench"),
            targets=[],
            session=transport,
            environ={**shared, "EDGE_PUBLISH_KIND": "repo", "EDGE_SITE": "bench", "EDGE_NAME": edge},
        )
        repo_paths.append(result["files"])
    assert release_refs == ["edge-share-bench-pi-bacnet", "edge-share-bench-pi-niagara"]
    assert repo_paths[0] != repo_paths[1]
    assert repo_paths[0] == ["share/bench/pi-bacnet/model.ttl", "share/bench/pi-bacnet/export.zip"]
    assert repo_paths[1] == ["share/bench/pi-niagara/model.ttl", "share/bench/pi-niagara/export.zip"]
    assert TOKEN not in json.dumps({"refs": release_refs, "paths": repo_paths})


def test_streaming_response_is_a_file_object():
    from edge_microdot.share import streaming_response

    response = streaming_response(
        b"abc",
        content_type="text/plain",
        filename="a.txt",
        modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert hasattr(response.body, "read")
    assert response.body.read() == b"abc"
    response.body.close()
    assert response.headers["ETag"].startswith('"')
    assert response.headers["Last-Modified"]
