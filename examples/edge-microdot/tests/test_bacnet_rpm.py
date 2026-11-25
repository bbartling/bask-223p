from __future__ import annotations

import asyncio

from edge_microdot.models import PointTarget
from edge_microdot.sources.bacnet_rpm import (
    BacnetRpmSource,
    BacnetTransportError,
    DeviceInfo,
)


class _AnyAtomicFake:
    def __init__(self, inner):
        self._inner = inner

    def get_value(self):
        return self._inner


class _FakeTransport:
    def __init__(self, *, max_apdu: int = 1476, reject_all_rpm: bool = False):
        self.max_apdu = max_apdu
        self.reject_all_rpm = reject_all_rpm
        self.rpm_calls: list[int] = []
        self.single_calls: list[tuple[str, str, int | None]] = []
        self.values: dict[tuple[str, str], object] = {}

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def who_is(self, **kwargs):
        return [
            DeviceInfo(
                device_instance=10001,
                address="192.168.1.50",
                max_apdu_length_accepted=self.max_apdu,
                segmentation_supported="noSegmentation",
            )
        ]

    async def read_property(self, address, object_identifier, property_identifier, *, array_index=None):
        self.single_calls.append((object_identifier, property_identifier, array_index))
        return self.values[(object_identifier, property_identifier)]

    async def read_property_multiple(self, address, items):
        self.rpm_calls.append(len(items))
        if self.reject_all_rpm:
            raise BacnetTransportError("AbortPDU", reason="segmentationNotSupported")
        out = []
        for obj_id, prop_id in items:
            out.append((obj_id, prop_id, None, self.values[(obj_id, prop_id)]))
        return out


def test_chunking_and_fallback():
    tr = _FakeTransport(max_apdu=480)
    targets = []
    for i in range(1, 21):
        oid = f"analog-input,{i}"
        tr.values[(oid, "present-value")] = _AnyAtomicFake(float(i))
        targets.append(PointTarget(site="s1", device="10001", object_id=oid, units="C"))
    src = BacnetRpmSource(targets, transport=tr, max_props=25)
    samples = asyncio.run(src.poll_once_async())
    assert len(samples) == 20
    assert tr.rpm_calls == [8, 8, 4]

def test_discover_targets_via_who_is():
    tr = _FakeTransport(max_apdu=1476)
    tr.values[("device,10001", "object-list")] = [
        "device,10001",
        "analog-input,1",
        "analog-value,2",
        "network-port,1",
    ]
    src = BacnetRpmSource([], transport=tr, local_address="192.168.204.59/24")
    targets = asyncio.run(src.discover_targets([10001], site="bench"))
    assert [t.object_id for t in targets] == ["analog-input,1", "analog-value,2"]
    assert all(t.device == "10001" for t in targets)
    assert all(t.device_address == "192.168.1.50" for t in targets)


def test_resolve_bacnet_local_address(monkeypatch):
    from edge_microdot.config import resolve_bacnet_local_address

    monkeypatch.delenv("EDGE_BACNET_ADDRESS", raising=False)
    monkeypatch.delenv("BACNET_LOCAL_ADDRESS", raising=False)
    assert resolve_bacnet_local_address(cli="192.168.204.59/24", config="0.0.0.0") == "192.168.204.59/24"
    monkeypatch.setenv("EDGE_BACNET_ADDRESS", "192.168.204.60/24")
    assert resolve_bacnet_local_address(cli=None, config="0.0.0.0") == "192.168.204.60/24"
    monkeypatch.delenv("EDGE_BACNET_ADDRESS", raising=False)
    try:
        resolve_bacnet_local_address(cli=None, config="0.0.0.0")
        raise AssertionError("expected ValueError for wildcard address")
    except ValueError as exc:
        assert "EDGE_BACNET_ADDRESS" in str(exc)
