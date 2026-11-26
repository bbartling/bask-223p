"""BACnet client for the edge app (BACpypes3 0.1.x, one asyncio loop).

Polling reads only configured points. Object-list discovery runs only when a
caller names one device. It returns object-name, object-type, units, and
description. It does not read present-value and it does not add those objects
to the poll set.

MS/TP and small controllers are paced per device: a max in-flight cap and a
gap between requests. RPM chunks shrink to the device's max APDU and
segmentation support. A chunk that aborts is split, then a single
ReadProperty. One timeout is retried once. Per-property errors stay on that
property.

Writes and priority releases are off unless ``EDGE_BACNET_WRITES_ENABLED`` is
set. Priorities 1 and 2 also need ``EDGE_BACNET_ALLOW_PRIORITY_1_2``. Every
write and release is appended to the audit log. Route-aware addressing is on
so a Who-Is source such as ``2000:7@192.168.204.200`` is preserved.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from edge_microdot.models import PointSample, PointTarget, utc_now_iso

from edge_microdot.models import PointSample, PointTarget, utc_now_iso


def _to_kebab(s: str) -> str:
    s = s.replace("_", "-")
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", s)
    return s.casefold()


def _object_type_name(typ: Any) -> str:
    if typ is None:
        return ""
    for attr in ("name", "attr"):
        val = getattr(typ, attr, None)
        if isinstance(val, str) and val and not val.startswith("<"):
            return _to_kebab(val)
    s = str(typ).strip()
    if s.startswith("<") and ":" in s:
        s = s.strip("<>").split(":", 1)[-1].strip()
    if "." in s and s.split(".", 1)[0].isalpha():
        s = s.rsplit(".", 1)[-1]
    return _to_kebab(s)


def normalize_discovered_object_id(raw: Any) -> str | None:
    """Normalize a Who-Is / object-list entry to ``type,instance``."""
    if raw is None:
        return None
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        typ_s = _object_type_name(raw[0])
        inst_s = str(raw[1]).strip().strip("]")
        if typ_s and inst_s.isdigit():
            return f"{typ_s},{inst_s}"
        return None
    s = str(raw).strip().strip("()")
    # "[<ObjectType: analog-value>, 1]" or "analog-input,1168"
    tagged = re.search(r"ObjectType:\s*([A-Za-z0-9_-]+)\s*>?,\s*(\d+)", s)
    if tagged:
        return f"{tagged.group(1).replace('_', '-')},{tagged.group(2)}"
    if "," not in s:
        return None
    typ, inst = s.split(",", 1)
    typ_s = _object_type_name(typ)
    inst_s = inst.strip().strip("]")
    if typ_s and inst_s.isdigit():
        return f"{typ_s},{inst_s}"
    return None




@dataclass(slots=True)
class DeviceInfo:
    device_instance: int
    address: str
    max_apdu_length_accepted: int | None = None
    segmentation_supported: str | None = None
    vendor_id: int | None = None
    object_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_instance": self.device_instance,
            "address": self.address,
            "max_apdu_length_accepted": self.max_apdu_length_accepted,
            "segmentation_supported": self.segmentation_supported,
            "vendor_id": self.vendor_id,
            "object_name": self.object_name,
        }


class BacnetTransport(Protocol):
    """Minimal async BACnet transport interface (real BACpypes3 or test fake)."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def who_is(
        self,
        *,
        low_limit: int | None = None,
        high_limit: int | None = None,
        address: str | None = None,
        timeout: float | None = None,
    ) -> list[DeviceInfo]: ...

    async def read_property(
        self,
        address: str,
        object_identifier: str,
        property_identifier: str,
        *,
        array_index: int | None = None,
    ) -> Any: ...

    async def read_property_multiple(
        self,
        address: str,
        items: Sequence[tuple[str, str]],
    ) -> list[tuple[str, str, int | None, Any]]: ...


class BacnetTransportError(RuntimeError):
    """Raised on BACnet Abort / Reject / Error / timeout."""

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class WriteDisabled(PermissionError):
    """EDGE_BACNET_WRITES_ENABLED is off."""


class PriorityRejected(ValueError):
    """Priority is outside 1..16, or 1..2 without the override flag."""


_DEVICE_INFO_PROPS = (
    "object-name",
    "description",
    "vendor-name",
    "vendor-identifier",
    "model-name",
    "firmware-revision",
    "application-software-version",
    "location",
    "protocol-version",
    "protocol-revision",
    "max-apdu-length-accepted",
    "segmentation-supported",
    "system-status",
)
_POINT_CATALOG_PROPS = ("object-name", "description", "units")
_INSTANCE_MAX = 4_194_303


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def validate_priority(priority: int, *, allow_life_safety: bool) -> int:
    """Accept 1..16. Priorities 1 and 2 stay closed unless allow_life_safety."""
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise PriorityRejected("priority must be an integer from 1 to 16")
    if priority < 1 or priority > 16:
        raise PriorityRejected("priority must be an integer from 1 to 16")
    if priority in (1, 2) and not allow_life_safety:
        raise PriorityRejected(
            "priorities 1 and 2 require EDGE_BACNET_ALLOW_PRIORITY_1_2"
        )
    return priority


class AuditLog:
    """Append-only JSONL of write and release attempts. Keeps a short tail in memory."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, event: dict[str, Any]) -> dict[str, Any]:
        row = {"timestamp_utc": utc_now_iso(), **event}
        line = json.dumps(row, default=str, separators=(",", ":"))
        with self._lock:
            self.records.append(row)
            if len(self.records) > 200:
                del self.records[:-200]
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        return row


class DevicePacer:
    """Per-device cap on in-flight requests and a gap between request starts."""

    def __init__(self, *, max_inflight: int = 1, gap_s: float = 0.0) -> None:
        self.max_inflight = max(1, int(max_inflight))
        self.gap_s = max(0.0, float(gap_s))
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._gap_locks: dict[str, asyncio.Lock] = {}
        self._next: dict[str, float] = {}

    def slot(self, key: str) -> "_DeviceSlot":
        return _DeviceSlot(self, key or "*")


class _DeviceSlot:
    def __init__(self, pacer: DevicePacer, key: str) -> None:
        self.pacer = pacer
        self.key = key

    async def __aenter__(self) -> "_DeviceSlot":
        pacer = self.pacer
        if self.key not in pacer._sems:
            pacer._sems[self.key] = asyncio.Semaphore(pacer.max_inflight)
            pacer._gap_locks[self.key] = asyncio.Lock()
            pacer._next[self.key] = 0.0
        await pacer._sems[self.key].acquire()
        try:
            async with pacer._gap_locks[self.key]:
                loop = asyncio.get_running_loop()
                delay = pacer._next[self.key] - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                pacer._next[self.key] = loop.time() + pacer.gap_s
        except BaseException:
            pacer._sems[self.key].release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.pacer._sems[self.key].release()
        return False


def unwrap_bacnet_value(val: Any) -> tuple[Any, str]:
    """Unwrap BACpypes3 AnyAtomic / Atomic / ErrorType / Enum values into (value, status)."""
    if val is None:
        return None, "ok"

    # Duck-type ErrorType (has errorClass / errorCode) without requiring live sockets
    if hasattr(val, "errorClass") and hasattr(val, "errorCode"):
        return None, f"error:{val.errorClass}:{val.errorCode}"

    # AnyAtomic (BACpypes3 constructeddata wrapper returned by RPM)
    if hasattr(val, "get_value") and callable(val.get_value):
        try:
            inner = val.get_value()
            return unwrap_bacnet_value(inner)
        except Exception as exc:
            return None, f"error:unwrap:{exc}"

    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None, "error:non_finite"

    # PriorityValue / OptionalReal and the other optional-* choices. str() of
    # these is the default object repr, which is not a slot value.
    choice = getattr(val, "_choice", None)
    if isinstance(choice, str) and choice:
        if choice == "null":
            return None, "ok"
        return unwrap_bacnet_value(getattr(val, choice, None))

    # Null is a tuple. Treat it as an empty slot before the list walk.
    if type(val).__name__ == "Null":
        return None, "ok"

    # Boolean subclasses int, so it has to be checked before the number branch.
    if type(val).__name__ == "Boolean":
        return bool(val), "ok"

    if isinstance(val, (bool, int, float, str)):
        return val, "ok"

    if isinstance(val, (list, tuple)):
        items = [unwrap_bacnet_value(v)[0] for v in val]
        return items, "ok"

    # BACpypes3 Enumerated / ObjectIdentifier / primitives stringify cleanly
    if hasattr(val, "attr") and hasattr(val, "__str__"):
        return str(val), "ok"

    return str(val), "ok"


def _is_segmentation_abort(exc: Exception) -> bool:
    reason = str(getattr(exc, "reason", "") or exc).casefold()
    return "segmentation" in reason or "buffer" in reason or "too-long" in reason


def _pdu_reason(exc: BaseException) -> str:
    reason = getattr(exc, "apduAbortRejectReason", None)
    if reason is not None:
        return str(reason)
    if hasattr(exc, "errorClass") and hasattr(exc, "errorCode"):
        return f"{exc.errorClass}:{exc.errorCode}"
    return str(exc)


def _raise_if_nack(result: Any) -> None:
    """BACpypes3 sometimes returns an Error/Reject/Abort instead of raising it."""
    if result is None:
        return
    name = type(result).__name__
    if name in {"AbortPDU", "RejectPDU", "ErrorPDU", "ErrorRejectAbortNack"}:
        raise BacnetTransportError(str(result), reason=_pdu_reason(result))


class Bacpypes3Transport:
    """Real BACpypes3 Application transport (only opened on start())."""

    def __init__(
        self,
        *,
        local_address: str = "0.0.0.0",
        device_instance: int = 999999,
        io_timeout_s: float = 8.0,
        route_aware: bool = True,
        foreign: str | None = None,
        foreign_ttl: int = 30,
    ) -> None:
        self.local_address = local_address
        self.device_instance = device_instance
        self.io_timeout_s = float(io_timeout_s)
        self.route_aware = route_aware
        self.foreign = (foreign or "").strip() or None
        self.foreign_ttl = int(foreign_ttl)
        self.app: Any = None

    async def start(self) -> None:
        if self.app is not None:
            return
        import argparse

        from bacpypes3.app import Application
        from bacpypes3.settings import settings

        # Route-aware keeps ``2000:7@192.168.204.200`` on the I-Am source.
        # BACpypes3's own env parser only accepts true/false/set/reset.
        settings.route_aware = bool(self.route_aware)

        # BACpypes3 0.1.x must be built with from_args/from_object_list so the
        # application service access point is linked. Application(device, address)
        # leaves elementService unset and Who-Is fails. Call this from a running
        # event loop. The same loop serves HTTP and the poll task.
        ns = argparse.Namespace(
            vendoridentifier=999,
            instance=int(self.device_instance),
            name="edge-microdot-client",
            address=self.local_address,
            foreign=self.foreign,
            network=0,
            bbmd=None,
            ttl=self.foreign_ttl,
        )
        self.app = Application.from_args(ns)

    async def close(self) -> None:
        if self.app is not None:
            self.app.close()
            self.app = None

    async def _await(self, awaitable: Any) -> Any:
        try:
            result = await asyncio.wait_for(awaitable, timeout=self.io_timeout_s)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            raise BacnetTransportError("timeout", reason="timeout") from exc
        except BacnetTransportError:
            raise
        except Exception as exc:
            raise BacnetTransportError(str(exc), reason=_pdu_reason(exc)) from exc
        except BaseException as exc:
            # ErrorRejectAbortNack subclasses BaseException, not Exception.
            if type(exc).__name__ in {"AbortPDU", "RejectPDU", "ErrorPDU", "ErrorRejectAbortNack"}:
                raise BacnetTransportError(str(exc), reason=_pdu_reason(exc)) from exc
            raise
        _raise_if_nack(result)
        return result

    async def who_is(
        self,
        *,
        low_limit: int | None = None,
        high_limit: int | None = None,
        address: str | None = None,
        timeout: float | None = None,
    ) -> list[DeviceInfo]:
        await self.start()
        from bacpypes3.pdu import Address

        dest = Address(address) if address else None
        if low_limit is not None and high_limit is None:
            high_limit = low_limit
        if high_limit is not None and low_limit is None:
            low_limit = high_limit
        who_timeout = 3.0 if timeout is None else float(timeout)
        # who_is has its own I-Am collection timer. Don't also cut it at io_timeout_s.
        previous = self.io_timeout_s
        self.io_timeout_s = max(previous, who_timeout + 1.0)
        try:
            i_ams = await self._await(
                self.app.who_is(low_limit, high_limit, dest, timeout=who_timeout)
            )
        finally:
            self.io_timeout_s = previous
        out: list[DeviceInfo] = []
        for iam in i_ams or []:
            dev_inst = int(iam.iAmDeviceIdentifier[1])
            src_addr = str(iam.pduSource)
            max_apdu = int(getattr(iam, "maxAPDULengthAccepted", 0) or 0) or None
            seg = str(getattr(iam, "segmentationSupported", "") or "") or None
            vendor = int(getattr(iam, "vendorID", 0) or 0) or None
            out.append(
                DeviceInfo(
                    device_instance=dev_inst,
                    address=src_addr,
                    max_apdu_length_accepted=max_apdu,
                    segmentation_supported=seg,
                    vendor_id=vendor,
                )
            )
        return out

    async def read_property(
        self,
        address: str,
        object_identifier: str,
        property_identifier: str,
        *,
        array_index: int | None = None,
    ) -> Any:
        await self.start()
        from bacpypes3.pdu import Address
        from bacpypes3.primitivedata import ObjectIdentifier

        return await self._await(
            self.app.read_property(
                Address(address),
                ObjectIdentifier(object_identifier),
                property_identifier,
                array_index=array_index,
            )
        )

    async def read_property_multiple(
        self,
        address: str,
        items: Sequence[tuple[str, str]],
    ) -> list[tuple[str, str, int | None, Any]]:
        await self.start()
        from bacpypes3.pdu import Address
        from bacpypes3.primitivedata import ObjectIdentifier

        spec: list[Any] = []
        for obj_id, prop_id in items:
            spec.append(ObjectIdentifier(obj_id))
            spec.append([prop_id])
        resp = await self._await(self.app.read_property_multiple(Address(address), spec))
        rows: list[tuple[str, str, int | None, Any]] = []
        for obj_id, prop_id, arr_idx, val in resp:
            rows.append((str(obj_id), str(prop_id), arr_idx, val))
        return rows

    async def write_property(
        self,
        address: str,
        object_identifier: str,
        property_identifier: str,
        value: Any,
        *,
        priority: int | None = None,
        array_index: int | None = None,
    ) -> None:
        await self.start()
        from bacpypes3.pdu import Address
        from bacpypes3.primitivedata import Null, ObjectIdentifier

        encoded = Null(()) if value is None else value
        await self._await(
            self.app.write_property(
                Address(address),
                ObjectIdentifier(object_identifier),
                property_identifier,
                encoded,
                array_index,
                priority,
            )
        )


class BacnetRpmSource:
    """BACnet polling and on-request discovery. Polling uses configured targets only."""

    def __init__(
        self,
        targets: Sequence[PointTarget],
        *,
        transport: BacnetTransport | None = None,
        local_address: str = "0.0.0.0",
        max_props: int = 25,
        discover_instances: Sequence[int] = (),
        site: str = "demo-site",
        io_timeout_s: float = 8.0,
        max_inflight: int | None = None,
        chunk_delay_s: float | None = None,
        whois_timeout_s: float | None = None,
        retries: int = 1,
        writes_enabled: bool | None = None,
        allow_priority_1_2: bool | None = None,
        audit_path: str | Path | None = None,
        route_aware: bool | None = None,
        foreign: str | None = None,
    ) -> None:
        self.targets = targets if isinstance(targets, list) else list(targets)
        self.site = site
        self.io_timeout_s = float(io_timeout_s)
        self.max_props = max(1, int(max_props))
        self.discover_instances = [int(x) for x in discover_instances]
        self.retries = max(0, int(retries))
        self.whois_timeout_s = (
            float(whois_timeout_s)
            if whois_timeout_s is not None
            else _env_float("EDGE_BACNET_WHOIS_TIMEOUT", 5.0)
        )
        gap = chunk_delay_s if chunk_delay_s is not None else _env_float("EDGE_BACNET_CHUNK_DELAY_S", 0.05)
        inflight = max_inflight if max_inflight is not None else _env_int("EDGE_BACNET_MAX_INFLIGHT", 1)
        self.pacer = DevicePacer(max_inflight=inflight, gap_s=gap)
        self.writes_enabled = (
            _env_bool("EDGE_BACNET_WRITES_ENABLED") if writes_enabled is None else writes_enabled
        )
        self.allow_priority_1_2 = (
            _env_bool("EDGE_BACNET_ALLOW_PRIORITY_1_2")
            if allow_priority_1_2 is None
            else allow_priority_1_2
        )
        if audit_path is None:
            audit_path = os.environ.get("EDGE_BACNET_AUDIT_LOG", "var/bacnet-audit.log")
        self.audit = AuditLog(audit_path)
        aware = _env_bool("EDGE_BACNET_ROUTE_AWARE", True) if route_aware is None else route_aware
        bbmd = foreign if foreign is not None else os.environ.get("EDGE_BACNET_BBMD")
        self.transport: BacnetTransport = transport or Bacpypes3Transport(
            local_address=local_address,
            io_timeout_s=self.io_timeout_s,
            route_aware=aware,
            foreign=bbmd,
            foreign_ttl=_env_int("EDGE_BACNET_FD_TTL", 30),
        )
        self.device_cache: dict[int, DeviceInfo] = {}
        for t in self.targets:
            inst = t.device_instance
            if inst is not None and t.device_address and inst not in self.device_cache:
                self.device_cache[inst] = DeviceInfo(device_instance=inst, address=t.device_address)

    async def close(self) -> None:
        await self.transport.close()

    def _check_limits(self, low_limit: int | None, high_limit: int | None) -> tuple[int | None, int | None]:
        if low_limit is not None and high_limit is None:
            high_limit = low_limit
        if high_limit is not None and low_limit is None:
            low_limit = high_limit
        if low_limit is None or high_limit is None:
            return low_limit, high_limit
        if low_limit > high_limit:
            raise ValueError("low_limit must be <= high_limit")
        if low_limit < 0 or high_limit > _INSTANCE_MAX:
            raise ValueError(f"device instance must be 0..{_INSTANCE_MAX}")
        return low_limit, high_limit

    async def who_is(
        self,
        *,
        low_limit: int | None = None,
        high_limit: int | None = None,
        address: str | None = None,
        timeout: float | None = None,
    ) -> list[DeviceInfo]:
        low_limit, high_limit = self._check_limits(low_limit, high_limit)
        async with self.pacer.slot(address or "*"):
            devices = await self.transport.who_is(
                low_limit=low_limit,
                high_limit=high_limit,
                address=address,
                timeout=self.whois_timeout_s if timeout is None else timeout,
            )
        for dev in devices:
            self.device_cache[dev.device_instance] = dev
        return devices

    async def _call(self, address: str, fn: Callable[[], Any]) -> Any:
        async with self.pacer.slot(address):
            last: BacnetTransportError | None = None
            for attempt in range(self.retries + 1):
                try:
                    return await fn()
                except BacnetTransportError as exc:
                    last = exc
                    timed_out = "timeout" in f"{exc.reason} {exc}".casefold()
                    if not timed_out or attempt >= self.retries:
                        raise
                    if self.pacer.gap_s:
                        await asyncio.sleep(self.pacer.gap_s)
            assert last is not None
            raise last

    async def _read_property(self, address: str, object_identifier: str, property_identifier: str, *, array_index: int | None = None) -> Any:
        return await self._call(
            address,
            lambda: self.transport.read_property(
                address, object_identifier, property_identifier, array_index=array_index
            ),
        )

    async def _read_multiple(self, address: str, items: Sequence[tuple[str, str]]) -> list[tuple[str, str, int | None, Any]]:
        return await self._call(address, lambda: self.transport.read_property_multiple(address, items))

    async def _resolve_device(self, device_instance: int) -> DeviceInfo | None:
        if device_instance in self.device_cache:
            return self.device_cache[device_instance]
        found = await self.who_is(low_limit=device_instance, high_limit=device_instance)
        return found[0] if found else self.device_cache.get(device_instance)

    def _effective_chunk_size(self, dev: DeviceInfo) -> int:
        cap = self.max_props
        apdu = dev.max_apdu_length_accepted
        if apdu is not None:
            if apdu <= 206:
                cap = min(cap, 4)
            elif apdu <= 480:
                cap = min(cap, 8)
            elif apdu <= 1024:
                cap = min(cap, 16)
        if dev.segmentation_supported and "no" in dev.segmentation_supported.casefold():
            cap = min(cap, 12)
        return max(1, cap)

    async def _read_chunk_with_fallback(
        self,
        address: str,
        batch: Sequence[PointTarget],
    ) -> list[tuple[PointTarget, Any, str]]:
        if len(batch) == 1:
            t = batch[0]
            try:
                resp = await self._read_multiple(address, [(t.object_id, t.property_id)])
                if resp:
                    val, status = unwrap_bacnet_value(resp[0][3])
                    return [(t, val, status)]
            except Exception:
                pass
            # Single read_property fallback (covers devices that reject RPM entirely)
            try:
                raw = await self._read_property(address, t.object_id, t.property_id)
                val, status = unwrap_bacnet_value(raw)
                return [(t, val, status)]
            except Exception as exc:
                return [(t, None, f"error:{exc}")]

        try:
            pairs = [(t.object_id, t.property_id) for t in batch]
            resp = await self._read_multiple(address, pairs)
            by_key: dict[tuple[str, str], tuple[Any, str]] = {}
            for obj_id, prop_id, _idx, raw in resp:
                by_key[(obj_id.casefold(), prop_id.casefold())] = unwrap_bacnet_value(raw)
            out: list[tuple[PointTarget, Any, str]] = []
            for t in batch:
                k = (t.object_id.casefold(), t.property_id.casefold())
                if k in by_key:
                    out.append((t, *by_key[k]))
                else:
                    # Missing from RPM answer -> single read fallback
                    try:
                        raw = await self._read_property(address, t.object_id, t.property_id)
                        val, status = unwrap_bacnet_value(raw)
                        out.append((t, val, status))
                    except Exception as exc:
                        out.append((t, None, f"error:{exc}"))
            return out
        except Exception:
            mid = len(batch) // 2
            left = await self._read_chunk_with_fallback(address, batch[:mid])
            right = await self._read_chunk_with_fallback(address, batch[mid:])
            return left + right

    async def poll_once_async(self) -> list[PointSample]:
        # Named devices are not expanded into poll targets. Object-list reads
        # happen only in discover_points / scan_devices for one device at a time.
        ts = utc_now_iso()
        by_device: dict[int, list[PointTarget]] = {}
        samples: list[PointSample] = []

        for t in self.targets:
            inst = t.device_instance
            if inst is None:
                samples.append(
                    PointSample(
                        timestamp_utc=ts,
                        site=t.site,
                        source="bacnet-rpm",
                        device=t.device,
                        object=t.object_id,
                        property=t.property_id,
                        value=None,
                        units=t.units,
                        status="error:invalid_device_instance",
                    )
                )
                continue
            by_device.setdefault(inst, []).append(t)

        for inst, dev_targets in by_device.items():
            dev = await self._resolve_device(inst)
            if dev is None:
                for t in dev_targets:
                    samples.append(
                        PointSample(
                            timestamp_utc=ts,
                            site=t.site,
                            source="bacnet-rpm",
                            device=t.device,
                            object=t.object_id,
                            property=t.property_id,
                            value=None,
                            units=t.units,
                            status="error:device_not_found",
                        )
                    )
                continue

            chunk_size = self._effective_chunk_size(dev)
            for i in range(0, len(dev_targets), chunk_size):
                batch = dev_targets[i : i + chunk_size]
                results = await self._read_chunk_with_fallback(dev.address, batch)
                for t, val, status in results:
                    samples.append(
                        PointSample(
                            timestamp_utc=ts,
                            site=t.site,
                            source="bacnet-rpm",
                            device=t.device,
                            object=t.object_id,
                            property=t.property_id,
                            value=val,
                            units=t.units,
                            status=status,
                        )
                    )
        return samples

    def poll_once(self) -> list[PointSample]:
        return asyncio.run(self.poll_once_async())

    async def discover_objects(self, device_instance: int) -> list[str]:
        """Read a device's object-list, falling back to index 0 + 1..N on segmentation abort."""
        dev = await self._resolve_device(device_instance)
        if dev is None:
            raise BacnetTransportError(f"Device {device_instance} not found via Who-Is")
        dev_obj = f"device,{device_instance}"
        try:
            raw = await self._read_property(dev.address, dev_obj, "object-list")
            val, status = unwrap_bacnet_value(raw)
            if status == "ok" and isinstance(val, list):
                return [oid for x in val if (oid := normalize_discovered_object_id(x))]
        except Exception as exc:
            if not _is_segmentation_abort(exc):
                raise

        length_raw = await self._read_property(
            dev.address, dev_obj, "object-list", array_index=0
        )
        length_val, status = unwrap_bacnet_value(length_raw)
        if status != "ok" or not isinstance(length_val, int):
            raise BacnetTransportError(f"Unable to read object-list[0] length: {status}")

        objects: list[str] = []
        for idx in range(1, length_val + 1):
            item_raw = await self._read_property(
                dev.address, dev_obj, "object-list", array_index=idx
            )
            item_val, item_status = unwrap_bacnet_value(item_raw)
            if item_status == "ok" and item_val is not None:
                oid = normalize_discovered_object_id(item_val)
                if oid:
                    objects.append(oid)
        return objects

    async def discover_targets(
        self,
        device_instances: Sequence[int],
        *,
        site: str,
        skip_types: frozenset[str] = frozenset({"device", "network-port"}),
    ) -> list[PointTarget]:
        """Build poll targets from each named device's object-list.

        The poll loop does not call this. Using it on a live controller turns
        the whole object-list into present-value polls. Prefer discover_points,
        which lists name, type, units, and description and leaves the poll set
        unchanged.
        """
        wanted = [int(x) for x in device_instances]
        if not wanted:
            self.targets = []
            return []
        targets: list[PointTarget] = []
        for inst in wanted:
            dev = await self._resolve_device(inst)
            if dev is None:
                continue
            objects = await self.discover_objects(inst)
            for raw in objects:
                obj_id = normalize_discovered_object_id(raw)
                if obj_id is None:
                    continue
                typ = obj_id.split(",", 1)[0].casefold()
                if typ in skip_types:
                    continue
                targets.append(
                    PointTarget(
                        site=site,
                        device=str(inst),
                        object_id=obj_id,
                        device_address=dev.address,
                        label=obj_id,
                    )
                )
        self.targets.clear()
        self.targets.extend(targets)
        return list(self.targets)

    async def read_properties(
        self,
        device_instance: int,
        reads: Sequence[tuple[str, str]],
        *,
        address: str | None = None,
    ) -> list[dict[str, Any]]:
        """ReadPropertyMultiple for one device, chunked to its max APDU."""
        dev = await self._resolve_device(device_instance)
        if address:
            dev = DeviceInfo(
                device_instance=device_instance,
                address=address,
                max_apdu_length_accepted=None if dev is None else dev.max_apdu_length_accepted,
                segmentation_supported=None if dev is None else dev.segmentation_supported,
                vendor_id=None if dev is None else dev.vendor_id,
                object_name="" if dev is None else dev.object_name,
            )
            self.device_cache[device_instance] = dev
        if dev is None:
            raise BacnetTransportError(f"Device {device_instance} not found via Who-Is")
        chunk = self._effective_chunk_size(dev)
        rows: list[dict[str, Any]] = []
        items = [(str(obj), str(prop)) for obj, prop in reads]
        for start in range(0, len(items), chunk):
            batch = items[start : start + chunk]
            rows.extend(await self._read_pairs(dev.address, batch))
        return rows

    async def _read_pairs(self, address: str, batch: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
        if len(batch) == 1:
            obj_id, prop_id = batch[0]
            try:
                resp = await self._read_multiple(address, [(obj_id, prop_id)])
                if resp:
                    val, status = unwrap_bacnet_value(resp[0][3])
                    return [_read_row(obj_id, prop_id, val, status)]
            except Exception:
                pass
            try:
                raw = await self._read_property(address, obj_id, prop_id)
                val, status = unwrap_bacnet_value(raw)
                return [_read_row(obj_id, prop_id, val, status)]
            except Exception as exc:
                return [_read_row(obj_id, prop_id, None, f"error:{exc}")]
        try:
            resp = await self._read_multiple(address, batch)
            by_key: dict[tuple[str, str], tuple[Any, str]] = {}
            for obj_id, prop_id, _idx, raw in resp:
                by_key[(str(obj_id).casefold(), str(prop_id).casefold())] = unwrap_bacnet_value(raw)
            out: list[dict[str, Any]] = []
            for obj_id, prop_id in batch:
                found = by_key.get((obj_id.casefold(), prop_id.casefold()))
                if found is None:
                    try:
                        raw = await self._read_property(address, obj_id, prop_id)
                        val, status = unwrap_bacnet_value(raw)
                    except Exception as exc:
                        val, status = None, f"error:{exc}"
                    out.append(_read_row(obj_id, prop_id, val, status))
                else:
                    out.append(_read_row(obj_id, prop_id, found[0], found[1]))
            return out
        except Exception:
            mid = len(batch) // 2
            left = await self._read_pairs(address, batch[:mid])
            right = await self._read_pairs(address, batch[mid:])
            return left + right

    async def read_device_info(self, device_instance: int, *, address: str | None = None) -> dict[str, Any]:
        """Read device-object properties. Does not read object-list."""
        dev = await self._resolve_device(device_instance)
        if address and (dev is None or dev.address != address):
            dev = DeviceInfo(device_instance=device_instance, address=address)
            self.device_cache[device_instance] = dev
        if dev is None:
            raise BacnetTransportError(f"Device {device_instance} not found via Who-Is")
        dev_obj = f"device,{device_instance}"
        rows = await self.read_properties(
            device_instance,
            [(dev_obj, prop) for prop in _DEVICE_INFO_PROPS],
            address=dev.address,
        )
        props = {row["property"]: {"value": row["value"], "status": row["status"]} for row in rows}
        name = props.get("object-name", {}).get("value")
        if isinstance(name, str) and name:
            dev.object_name = name
        body = dev.to_dict()
        body["properties"] = props
        return body

    async def discover_points(self, device_instance: int, *, address: str | None = None) -> dict[str, Any]:
        """List one device's objects. Does not read present-value or change poll targets."""
        objects = await self.discover_objects(device_instance) if address is None else await self._discover_at(
            device_instance, address
        )
        reads = [(oid, prop) for oid in objects for prop in _POINT_CATALOG_PROPS]
        rows = await self.read_properties(device_instance, reads, address=address) if reads else []
        by_obj: dict[str, dict[str, Any]] = {}
        for oid in objects:
            typ, inst_s = oid.split(",", 1)
            by_obj[oid] = {
                "object_identifier": oid,
                "object_type": typ,
                "instance": int(inst_s),
                "object_name": None,
                "units": None,
                "description": None,
                "errors": {},
            }
        for row in rows:
            slot = by_obj.get(row["object"])
            if slot is None:
                continue
            field = {"object-name": "object_name", "units": "units", "description": "description"}[row["property"]]
            if row["status"] == "ok":
                slot[field] = row["value"]
            else:
                slot["errors"][row["property"]] = row["status"]
        dev = self.device_cache.get(device_instance)
        return {
            "device_instance": device_instance,
            "address": None if dev is None else dev.address,
            "count": len(objects),
            "objects": [by_obj[oid] for oid in objects],
        }

    async def _discover_at(self, device_instance: int, address: str) -> list[str]:
        self.device_cache[device_instance] = DeviceInfo(device_instance=device_instance, address=address)
        return await self.discover_objects(device_instance)

    async def read_priority_array(
        self,
        device_instance: int,
        object_identifier: str,
        *,
        address: str | None = None,
    ) -> dict[str, Any]:
        """Read all 16 priority slots and relinquish-default. Uses ReadProperty, not RPM."""
        dev = await self._resolve_device(device_instance)
        if address:
            dev = DeviceInfo(device_instance=device_instance, address=address)
            self.device_cache[device_instance] = dev
        if dev is None:
            raise BacnetTransportError(f"Device {device_instance} not found via Who-Is")
        slots: list[Any] = [None] * 16
        slot_status: dict[str, str] = {}
        whole_ok = False
        try:
            raw = await self._read_property(dev.address, object_identifier, "priority-array")
            val, status = unwrap_bacnet_value(raw)
            if status == "ok" and isinstance(val, list) and len(val) >= 16:
                for index in range(16):
                    slots[index] = _priority_slot(val[index])
                    slot_status[str(index + 1)] = "ok"
                whole_ok = True
            else:
                raise BacnetTransportError("priority-array incomplete", reason="segmentationNotSupported")
        except Exception as exc:
            if whole_ok:
                raise
            if not _is_segmentation_abort(exc) and "incomplete" not in str(exc):
                # Still fall back to indexes. Some devices reject the whole array.
                pass
            for index in range(1, 17):
                try:
                    raw = await self._read_property(
                        dev.address, object_identifier, "priority-array", array_index=index
                    )
                    val, status = unwrap_bacnet_value(raw)
                    slots[index - 1] = _priority_slot(val) if status == "ok" else None
                    slot_status[str(index)] = status
                except Exception as slot_exc:
                    slots[index - 1] = None
                    slot_status[str(index)] = f"error:{slot_exc}"
        try:
            raw = await self._read_property(dev.address, object_identifier, "relinquish-default")
            rel, rel_status = unwrap_bacnet_value(raw)
        except Exception as exc:
            rel, rel_status = None, f"error:{exc}"
        return {
            "device_instance": device_instance,
            "address": dev.address,
            "object_identifier": object_identifier,
            "priority_array": slots,
            "slot_status": slot_status,
            "relinquish_default": rel,
            "relinquish_default_status": rel_status,
        }

    async def write_property(
        self,
        device_instance: int,
        object_identifier: str,
        value: Any,
        *,
        property_identifier: str = "present-value",
        priority: int = 8,
        user: str = "",
        address: str | None = None,
    ) -> dict[str, Any]:
        return await self._write(
            device_instance,
            object_identifier,
            property_identifier,
            value,
            priority=priority,
            user=user,
            address=address,
            release=False,
        )

    async def release_property(
        self,
        device_instance: int,
        object_identifier: str,
        *,
        property_identifier: str = "present-value",
        priority: int = 8,
        user: str = "",
        address: str | None = None,
    ) -> dict[str, Any]:
        """Write NULL at priority, which relinquishes that slot."""
        return await self._write(
            device_instance,
            object_identifier,
            property_identifier,
            None,
            priority=priority,
            user=user,
            address=address,
            release=True,
        )

    async def _write(
        self,
        device_instance: int,
        object_identifier: str,
        property_identifier: str,
        value: Any,
        *,
        priority: int,
        user: str,
        address: str | None,
        release: bool,
    ) -> dict[str, Any]:
        checked = validate_priority(priority, allow_life_safety=self.allow_priority_1_2)
        action = "release" if release else "write"
        base = {
            "action": action,
            "user": user,
            "device_instance": device_instance,
            "object_identifier": object_identifier,
            "property_identifier": property_identifier,
            "priority": checked,
            "value": None if release else value,
        }
        if not self.writes_enabled:
            self.audit.record({**base, "outcome": "denied", "reason": "EDGE_BACNET_WRITES_ENABLED"})
            raise WriteDisabled("BACnet writes are off (EDGE_BACNET_WRITES_ENABLED)")
        dev = await self._resolve_device(device_instance)
        if address:
            dev = DeviceInfo(device_instance=device_instance, address=address)
            self.device_cache[device_instance] = dev
        if dev is None:
            self.audit.record({**base, "outcome": "error", "reason": "device_not_found"})
            raise BacnetTransportError(f"Device {device_instance} not found via Who-Is")
        writer = getattr(self.transport, "write_property", None)
        if writer is None:
            self.audit.record({**base, "outcome": "error", "reason": "transport_read_only"})
            raise BacnetTransportError("transport does not support WriteProperty")
        try:
            async with self.pacer.slot(dev.address):
                await writer(
                    dev.address,
                    object_identifier,
                    property_identifier,
                    None if release else value,
                    priority=checked,
                )
        except Exception as exc:
            self.audit.record({**base, "outcome": "error", "reason": str(exc), "address": dev.address})
            raise
        self.audit.record({**base, "outcome": "ok", "address": dev.address})
        return {**base, "outcome": "ok", "address": dev.address}

    async def scan_devices(
        self,
        device_instances: Sequence[int],
        *,
        timeout: float | None = None,
        address: str | None = None,
    ) -> dict[str, Any]:
        """Read-only catalog of named devices. Does not read present-value."""
        devices: list[dict[str, Any]] = []
        for inst in device_instances:
            entry: dict[str, Any] = {"device_instance": int(inst)}
            try:
                found = await self.who_is(
                    low_limit=int(inst),
                    high_limit=int(inst),
                    address=address,
                    timeout=timeout,
                )
                match = next((dev for dev in found if dev.device_instance == int(inst)), None)
                if match is None:
                    entry["error"] = "no I-Am"
                    devices.append(entry)
                    continue
                entry["i_am"] = match.to_dict()
                entry["device"] = await self.read_device_info(int(inst))
                entry["points"] = await self.discover_points(int(inst))
            except Exception as exc:
                entry["error"] = str(exc)
            devices.append(entry)
        return {"read_only": True, "present_value": False, "devices": devices}


def _read_row(object_id: str, property_id: str, value: Any, status: str) -> dict[str, Any]:
    return {"object": object_id, "property": property_id, "value": value, "status": status}


def _priority_slot(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict) and ("null" in value or value.get("null") == []):
        return None
    choice = getattr(value, "_choice", None)
    if choice == "null":
        return None
    if choice:
        raw = getattr(value, choice, None)
        unwrapped, status = unwrap_bacnet_value(raw)
        return None if status != "ok" else unwrapped
    unwrapped, status = unwrap_bacnet_value(value)
    if status != "ok":
        return None
    if isinstance(unwrapped, str) and unwrapped.casefold() in {"null", "none"}:
        return None
    return unwrapped
