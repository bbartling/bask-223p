"""Shared point target and telemetry sample dataclasses."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_bacnet_ref(device: str | int, object_id: str) -> str:
    dev = str(device).strip()
    if dev.startswith("device,"):
        dev = dev.split(",", 1)[1]
    obj = str(object_id).strip()
    if dev and "," in obj:
        return f"bacnet://{dev}/{obj}"
    return ""


@dataclass(slots=True)
class PointTarget:
    site: str
    device: str
    object_id: str
    property_id: str = "present-value"
    units: str = ""
    label: str = ""
    ord_path: str = ""
    point_uri: str = ""
    device_address: str = ""
    ref: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.ref:
            self.ref = make_bacnet_ref(self.device, self.object_id)

    @property
    def device_instance(self) -> int | None:
        raw = str(self.device).strip()
        if raw.startswith("device,"):
            raw = raw.split(",", 1)[1]
        return int(raw) if raw.isdigit() else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PointSample:
    timestamp_utc: str
    site: str
    source: str
    device: str
    object: str
    property: str
    value: Any
    units: str = ""
    status: str = "ok"
    ref: str = ""

    def __post_init__(self) -> None:
        if not self.ref:
            self.ref = make_bacnet_ref(self.device, self.object)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
