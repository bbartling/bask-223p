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