"""Point targets and telemetry samples with bacnet:// and niagara:// refs."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from bask_223p.bacnet.refs import niagara_object_uri


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


def make_point_ref(
    *,
    device: str,
    object_id: str,
    ord_path: str = "",
    point_name: str = "",
) -> str:
    bac = make_bacnet_ref(device, object_id)
    if bac:
        return bac
    if ord_path or point_name:
        return niagara_object_uri(ord_path, point_name)
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
    min_value: float | None = None
    max_value: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.ref:
            self.ref = make_point_ref(
                device=self.device,
                object_id=self.object_id,
                ord_path=self.ord_path,
                point_name=self.label,
            )

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