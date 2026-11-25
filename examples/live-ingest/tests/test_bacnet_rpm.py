from __future__ import annotations

import asyncio
from dataclasses import dataclass

from live_ingest.models import PointTarget
from live_ingest.sources.bacnet_rpm import (
    BacnetRpmSource,
    BacnetTransportError,
    DeviceInfo,
)


class _AnyAtomicFake:
    def __init__(self, inner):
        self._inner = inner

    def get_value(self):
        return self._inner


@dataclass
class _ErrorTypeFake:
    errorClass: str
    errorCode: str


class _FakeTransport:
    def __init__(self, *, max_apdu: int = 1476, seg: str = "segmentedBoth", fail_rpm_over: int = 999, reject_all_rpm: bool = False):
        self.max_apdu = max_apdu
        self.seg = seg
        self.fail_rpm_over = fail_rpm_over
        self.reject_all_rpm = reject_all_rpm
        self.rpm_calls: list[int] = []
        self.single_calls: list[tuple[str, str, int | None]] = []
        self.values: dict[tuple[str, str], object] = {}

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def who_is(self, *, low_limit=None, high_limit=None, address=None):
        return [
            DeviceInfo(
                device_instance=10001,
                address="192.168.1.50",
                max_apdu_length_accepted=self.max_apdu,
                segmentation_supported=self.seg,
                vendor_id=260,
            )
        ]

    async def read_property(self, address: str, object_identifier: str, property_identifier: str, *, array_index=None):
        self.single_calls.append((object_identifier, property_identifier, array_index))
        if object_identifier == "device,10001" and property_identifier == "object-list":
            if array_index is None:
                raise BacnetTransportError("AbortPDU", reason="segmentationNotSupported")
            if array_index == 0:
                return 2
            return ["analog-input,1", "analog-value,1"][array_index - 1]
        return self.values[(object_identifier, property_identifier)]

    async def read_property_multiple(self, address: str, items):
        self.rpm_calls.append(len(items))
        if self.reject_all_rpm or len(items) > self.fail_rpm_over:
            raise BacnetTransportError("AbortPDU", reason="segmentationNotSupported")
        out = []
        for obj_id, prop_id in items:
            out.append((obj_id, prop_id, None, self.values[(obj_id, prop_id)]))
        return out


def test_rpm_chunking_and_apdu_budget():
    tr = _FakeTransport(max_apdu=480, seg="noSegmentation")
    targets = []
    for i in range(1, 21):
        oid = f"analog-input,{i}"
        tr.values[(oid, "present-value")] = _AnyAtomicFake(float(i) + 0.5)
        targets.append(PointTarget(site="s1", device="10001", object_id=oid, units="DEG_F"))

    src = BacnetRpmSource(targets, transport=tr, max_props=25)
    samples = src.poll_once()
    assert len(samples) == 20
    # max_apdu=480 caps chunk size to 8 (20 = 8 + 8 + 4)
    assert tr.rpm_calls == [8, 8, 4]
    assert all(s.status == "ok" and s.ref.startswith("bacnet://10001/") for s in samples)


def test_rpm_abort_splits_and_falls_back_to_single_read():
    tr = _FakeTransport(reject_all_rpm=True)
    tr.values[("analog-input,1", "present-value")] = 71.2
    tr.values[("analog-input,2", "present-value")] = _ErrorTypeFake("object", "unknownObject")
    tr.values[("analog-input,3", "present-value")] = float("nan")

    targets = [
        PointTarget(site="s1", device="10001", object_id="analog-input,1"),
        PointTarget(site="s1", device="10001", object_id="analog-input,2"),
        PointTarget(site="s1", device="10001", object_id="analog-input,3"),
    ]
    src = BacnetRpmSource(targets, transport=tr, max_props=25)
    samples = src.poll_once()
    assert samples[0].value == 71.2 and samples[0].status == "ok"
    assert samples[1].value is None and samples[1].status == "error:object:unknownObject"
    assert samples[2].value is None and samples[2].status == "error:non_finite"
    assert len(tr.single_calls) == 3


def test_object_list_segmentation_fallback():
    tr = _FakeTransport()
    src = BacnetRpmSource([], transport=tr)
    objs = asyncio.run(src.discover_objects(10001))
    assert objs == ["analog-input,1", "analog-value,1"]
