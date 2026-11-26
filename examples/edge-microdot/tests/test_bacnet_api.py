"""BACnet HTTP routes: token auth, read catalog, write gate."""
from __future__ import annotations

import asyncio
import base64

from edge_microdot.app import create_app
from edge_microdot.cache import HotCache
from edge_microdot.config import LoadedConfig
from edge_microdot.fdd import FddEngine
from edge_microdot.history_store import HistoryStore
from edge_microdot.security import load_auth_from_env
from edge_microdot.sources.bacnet_rpm import BacnetRpmSource, BacnetTransportError, DeviceInfo
from edge_microdot.sparql_kb import SparqlKb


class _ApiTransport:
    def __init__(self) -> None:
        self.writes = []
        self.present_reads = 0

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def who_is(self, **kwargs):
        low = kwargs.get("low_limit")
        if low not in (None, 5007):
            return []
        return [
            DeviceInfo(
                device_instance=5007,
                address="2000:7@192.168.204.200",
                max_apdu_length_accepted=480,
                segmentation_supported="noSegmentation",
                vendor_id=5,
            )
        ]

    async def read_property(self, address, object_identifier, property_identifier, *, array_index=None):
        if property_identifier == "present-value":
            self.present_reads += 1
        if property_identifier == "object-list" and array_index is None:
            raise BacnetTransportError("abort", reason="segmentationNotSupported")
        if property_identifier == "object-list" and array_index == 0:
            return 1
        if property_identifier == "object-list":
            return "analog-input,1"
        if property_identifier == "priority-array" and array_index is None:
            return [None] * 16
        if property_identifier == "relinquish-default":
            return 10
        values = {
            ("device,5007", "object-name"): "AHU",
            ("device,5007", "vendor-name"): "Bench",
            ("analog-input,1", "object-name"): "Zone temp",
            ("analog-input,1", "description"): "room",
            ("analog-input,1", "units"): "degrees-celsius",
            ("analog-input,1", "present-value"): 21.0,
        }
        return values.get((object_identifier, property_identifier), "")

    async def read_property_multiple(self, address, items):
        out = []
        for obj_id, prop_id in items:
            out.append((obj_id, prop_id, None, await self.read_property(address, obj_id, prop_id)))
        return out

    async def write_property(self, address, object_identifier, property_identifier, value, *, priority=None, array_index=None):
        self.writes.append((object_identifier, value, priority))


def _app(tmp_path, transport, **source_kw):
    source = BacnetRpmSource(
        [],
        transport=transport,
        discover_instances=[5007],
        chunk_delay_s=0,
        retries=0,
        audit_path=tmp_path / "audit.log",
        **source_kw,
    )
    history = HistoryStore(tmp_path / "h.duckdb")
    app = create_app(
        source=source,
        source_kind="bacnet",
        targets=[],
        cfg=LoadedConfig(site="bench"),
        hot=HotCache("dict"),
        history=history,
        fdd=FddEngine(history=history),
        sparql=SparqlKb(None),
        auth=load_auth_from_env(),
        site="bench",
        poll_interval_s=3600,
        flush_interval_s=3600,
        fdd_interval_s=3600,
    )
    return app, source, history


async def _token(client) -> str:
    raw = base64.b64encode(b"operator:operator-pass").decode()
    resp = await client.post("/auth/token", headers={"Authorization": f"Basic {raw}"})
    return resp.json["access_token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_bacnet_routes_require_a_token_and_do_not_poll_present_value(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGE_BACNET_WRITES_ENABLED", raising=False)
    from microdot.test_client import TestClient

    transport = _ApiTransport()
    app, _source, history = _app(tmp_path, transport)

    async def _run():
        client = TestClient(app)
        denied = await client.get("/api/bacnet/whois?low=5007&high=5007")
        assert denied.status_code == 401
        token = await _token(client)
        who = await client.get("/api/bacnet/whois?low=5007&high=5007", headers=_bearer(token))
        assert who.status_code == 200
        assert who.json["devices"][0]["address"] == "2000:7@192.168.204.200"
        assert who.json["devices"][0]["object_name"] == ""
        catalog = await client.get("/api/bacnet/devices/5007/objects", headers=_bearer(token))
        assert catalog.status_code == 200
        assert catalog.json["objects"][0]["object_name"] == "Zone temp"
        assert catalog.json["objects"][0]["units"] == "degrees-celsius"
        read = await client.post(
            "/api/bacnet/read",
            headers=_bearer(token),
            body={"device": 5007, "object": "analog-input,1", "property": "present-value"},
        )
        assert read.status_code == 200
        assert read.json["results"][0]["value"] == 21.0
        pri = await client.get(
            "/api/bacnet/priority?device=5007&object=analog-output,1",
            headers=_bearer(token),
        )
        assert pri.status_code == 200
        assert len(pri.json["priority_array"]) == 16
        assert pri.json["relinquish_default"] == 10
        blocked = await client.post(
            "/api/bacnet/write",
            headers=_bearer(token),
            body={"device": 5007, "object": "analog-output,1", "property": "present-value", "value": 40, "priority": 8},
        )
        assert blocked.status_code == 403
        assert transport.writes == []
        listed = await client.get("/api/bacnet/devices", headers=_bearer(token))
        assert listed.status_code == 200
        assert listed.json["devices"][0]["object_name"] == "AHU"
        too_many = await client.post(
            "/api/bacnet/read",
            headers=_bearer(token),
            body={"device": 5007, "reads": [{"object": "analog-input,1", "property": "present-value"}] * 51},
        )
        assert too_many.status_code == 400
        monkeypatch.setenv("EDGE_BACNET_READ_MAX", "2")
        capped = await client.post(
            "/api/bacnet/read",
            headers=_bearer(token),
            body={"device": 5007, "reads": [{"object": "analog-input,1", "property": "present-value"}] * 3},
        )
        assert capped.status_code == 400

    try:
        asyncio.run(_run())
    finally:
        history.close()
    assert transport.present_reads == 1


def test_admin_write_and_priority_one_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGE_ROLES", "operator:admin")
    from microdot.test_client import TestClient

    transport = _ApiTransport()
    app, _source, history = _app(tmp_path, transport, writes_enabled=True, allow_priority_1_2=False)

    async def _run():
        client = TestClient(app)
        token = await _token(client)
        life = await client.post(
            "/api/bacnet/write",
            headers=_bearer(token),
            body={"device": 5007, "object": "analog-output,1", "value": 1, "priority": 1},
        )
        assert life.status_code == 400
        ok = await client.post(
            "/api/bacnet/write",
            headers=_bearer(token),
            body={"device": 5007, "object": "analog-output,1", "value": 40, "priority": 8},
        )
        assert ok.status_code == 200
        assert ok.json["outcome"] == "ok"
        released = await client.post(
            "/api/bacnet/release",
            headers=_bearer(token),
            body={"device": 5007, "object": "analog-output,1", "priority": 8},
        )
        assert released.status_code == 200
        assert released.json["action"] == "release"

    try:
        asyncio.run(_run())
    finally:
        history.close()
    assert transport.writes == [
        ("analog-output,1", 40, 8),
        ("analog-output,1", None, 8),
    ]
    text = (tmp_path / "audit.log").read_text()
    assert '"action":"write"' in text
    assert '"action":"release"' in text
