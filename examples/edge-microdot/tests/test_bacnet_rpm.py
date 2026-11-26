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

def test_poll_does_not_expand_named_devices_into_targets():
    tr = _FakeTransport()
    tr.values[("device,10001", "object-list")] = ["analog-input,1", "analog-value,2"]
    src = BacnetRpmSource([], transport=tr, discover_instances=[10001], chunk_delay_s=0, retries=0)
    samples = asyncio.run(src.poll_once_async())
    assert samples == []
    assert tr.single_calls == []
    assert tr.rpm_calls == []
    assert src.targets == []


def test_object_list_segmentation_fallback_and_point_catalog():
    class _Seg(_FakeTransport):
        async def read_property(self, address, object_identifier, property_identifier, *, array_index=None):
            self.single_calls.append((object_identifier, property_identifier, array_index))
            if property_identifier == "object-list" and array_index is None:
                raise BacnetTransportError("abort", reason="segmentationNotSupported")
            if property_identifier == "object-list" and array_index == 0:
                return 2
            if property_identifier == "object-list" and array_index == 1:
                return "analog-input,1"
            if property_identifier == "object-list" and array_index == 2:
                return "binary-output,3"
            return self.values[(object_identifier, property_identifier)]

    tr = _Seg()
    tr.values[("analog-input,1", "object-name")] = "SA Temp"
    tr.values[("analog-input,1", "description")] = "supply"
    tr.values[("analog-input,1", "units")] = "degrees-celsius"
    tr.values[("binary-output,3", "object-name")] = "Fan"
    tr.values[("binary-output,3", "description")] = ""
    tr.values[("binary-output,3", "units")] = _Err()
    src = BacnetRpmSource([], transport=tr, chunk_delay_s=0, retries=0)
    catalog = asyncio.run(src.discover_points(10001))
    assert catalog["count"] == 2
    assert catalog["objects"][0]["object_name"] == "SA Temp"
    assert catalog["objects"][0]["object_type"] == "analog-input"
    assert catalog["objects"][0]["units"] == "degrees-celsius"
    assert "units" in catalog["objects"][1]["errors"]
    assert all(call[1] != "present-value" for call in tr.single_calls)
    assert src.targets == []


class _Err:
    errorClass = "property"
    errorCode = "unknown-property"


def test_priority_slots_unwrap_choice_types():
    from bacpypes3.basetypes import OptionalReal, OptionalUnsigned, PriorityValue

    from edge_microdot.sources.bacnet_rpm import unwrap_bacnet_value

    number, status = unwrap_bacnet_value(OptionalReal(real=21.5))
    assert status == "ok" and number == 21.5
    empty, status = unwrap_bacnet_value(OptionalReal(null=()))
    assert status == "ok" and empty is None
    unsigned, status = unwrap_bacnet_value(OptionalUnsigned(unsigned=7))
    assert status == "ok" and unsigned == 7
    flag, status = unwrap_bacnet_value(PriorityValue(boolean=1))
    assert status == "ok" and flag is True
    text, status = unwrap_bacnet_value(PriorityValue(characterString="hi"))
    assert status == "ok" and text == "hi"

    class _Whole(_FakeTransport):
        async def read_property(self, address, object_identifier, property_identifier, *, array_index=None):
            if property_identifier == "priority-array":
                slots = [OptionalReal(null=()) for _ in range(16)]
                slots[0] = PriorityValue(boolean=0)
                slots[8] = OptionalReal(real=19.0)
                return slots
            if property_identifier == "relinquish-default":
                return OptionalReal(real=10.0).real
            raise AssertionError(property_identifier)

    src = BacnetRpmSource(
        [PointTarget(site="s", device="10001", object_id="analog-output,1", device_address="192.168.1.50")],
        transport=_Whole(),
        chunk_delay_s=0,
        retries=0,
    )
    body = asyncio.run(src.read_priority_array(10001, "analog-output,1"))
    assert body["priority_array"][0] is False
    assert body["priority_array"][8] == 19.0
    assert body["priority_array"].count(None) == 14
    assert "object at 0x" not in repr(body["priority_array"])


def test_priority_array_index_fallback_and_relinquish_default():
    class _Pri(_FakeTransport):
        async def read_property(self, address, object_identifier, property_identifier, *, array_index=None):
            self.single_calls.append((object_identifier, property_identifier, array_index))
            if property_identifier == "priority-array" and array_index is None:
                raise BacnetTransportError("abort", reason="bufferOverflow")
            if property_identifier == "priority-array":
                return None if array_index != 8 else 21.5
            if property_identifier == "relinquish-default":
                return 0
            raise AssertionError(property_identifier)

    tr = _Pri()
    src = BacnetRpmSource(
        [PointTarget(site="s", device="10001", object_id="analog-output,1", device_address="192.168.1.50")],
        transport=tr,
        chunk_delay_s=0,
        retries=0,
    )
    body = asyncio.run(src.read_priority_array(10001, "analog-output,1"))
    assert body["priority_array"][7] == 21.5
    assert body["priority_array"].count(None) == 15
    assert body["relinquish_default"] == 0
    assert len(body["priority_array"]) == 16


def test_write_gating_priority_and_audit(tmp_path):
    class _W(_FakeTransport):
        def __init__(self):
            super().__init__()
            self.writes = []

        async def write_property(self, address, object_identifier, property_identifier, value, *, priority=None, array_index=None):
            self.writes.append((object_identifier, property_identifier, value, priority))

    tr = _W()
    audit = tmp_path / "audit.log"
    src = BacnetRpmSource(
        [],
        transport=tr,
        chunk_delay_s=0,
        retries=0,
        writes_enabled=False,
        allow_priority_1_2=False,
        audit_path=audit,
    )
    src.device_cache[10001] = DeviceInfo(10001, "192.168.1.50")
    from edge_microdot.sources.bacnet_rpm import PriorityRejected, WriteDisabled

    try:
        asyncio.run(src.write_property(10001, "analog-output,1", 1, priority=1, user="op"))
        raise AssertionError("priority 1 should be rejected")
    except PriorityRejected:
        pass
    assert tr.writes == []
    try:
        asyncio.run(src.write_property(10001, "analog-output,1", 50, priority=8, user="op"))
        raise AssertionError("writes should be off")
    except WriteDisabled:
        pass
    assert tr.writes == []
    denied = [line for line in audit.read_text().splitlines() if line.strip()]
    assert len(denied) == 1
    assert '"outcome":"denied"' in denied[0]

    src.writes_enabled = True
    asyncio.run(src.write_property(10001, "analog-output,1", 50, priority=8, user="admin"))
    asyncio.run(src.release_property(10001, "analog-output,1", priority=8, user="admin"))
    assert tr.writes == [
        ("analog-output,1", "present-value", 50, 8),
        ("analog-output,1", "present-value", None, 8),
    ]
    lines = [line for line in audit.read_text().splitlines() if line.strip()]
    assert len(lines) == 3
    assert '"action":"release"' in lines[-1]
    src.allow_priority_1_2 = True
    asyncio.run(src.write_property(10001, "analog-output,1", 1, priority=2, user="admin"))
    assert tr.writes[-1][3] == 2


def test_rpm_chunk_splits_when_the_device_aborts():
    class _Abort(_FakeTransport):
        async def read_property_multiple(self, address, items):
            self.rpm_calls.append(len(items))
            if len(items) > 1:
                raise BacnetTransportError("abort", reason="segmentationNotSupported")
            return await super().read_property_multiple(address, items)

    tr = _Abort(max_apdu=1476)
    targets = []
    for i in range(1, 3):
        oid = f"analog-input,{i}"
        tr.values[(oid, "present-value")] = float(i)
        targets.append(PointTarget(site="s1", device="10001", object_id=oid, device_address="192.168.1.50"))
    src = BacnetRpmSource(targets, transport=tr, max_props=25, chunk_delay_s=0, retries=0)
    samples = asyncio.run(src.poll_once_async())
    assert [s.value for s in samples] == [1.0, 2.0]
    assert tr.rpm_calls[0] == 2
    assert 1 in tr.rpm_calls


def test_whois_requires_a_range_and_keeps_i_am_fields():
    tr = _FakeTransport()
    src = BacnetRpmSource([], transport=tr, chunk_delay_s=0, retries=0, whois_timeout_s=1)
    try:
        asyncio.run(src.who_is(low_limit=5007, high_limit=5000))
        raise AssertionError("inverted range should fail")
    except ValueError as exc:
        assert "low_limit" in str(exc)
    found = asyncio.run(src.who_is(low_limit=10001, high_limit=10001, timeout=2))
    assert found[0].to_dict()["max_apdu_length_accepted"] == 1476
    assert found[0].segmentation_supported == "noSegmentation"


def test_normalize_object_list_entries():
    from edge_microdot.sources.bacnet_rpm import normalize_discovered_object_id

    assert normalize_discovered_object_id(["analog-value", 1]) == "analog-value,1"
    assert normalize_discovered_object_id(["analogValue", 1]) == "analog-value,1"
    assert normalize_discovered_object_id("[<ObjectType: analog-value>, 1]") == "analog-value,1"
    assert normalize_discovered_object_id("analog-input,1168") == "analog-input,1168"
    assert normalize_discovered_object_id("device,54545") == "device,54545"
