"""Read-only BACnet ReadPropertyMultiple (RPM) source with chunking & APDU fallbacks.

Lessons applied from BACpypes3 RPM / APDU-budget practice:
- Group point reads by device and chunk ReadPropertyMultiple (default max_props=25).
- Shrink chunk budget automatically when a device reports a small max-apdu-length-accepted
  (e.g. <= 480 bytes on MS/TP controllers) or no segmentation support.
- On AbortPDU / RejectPDU / ErrorPDU / timeout for a chunk, split the chunk in half
  and retry; when down to 1 item, fall back to single ``read_property``.
- Preserve per-property ``ErrorType`` results inside RPM responses without failing
  healthy points in the same batch.
- For ``object-list`` discovery on non-segmenting devices, fall back on
  ``segmentationNotSupported`` to reading array index ``0`` (length) and ``1..N``.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from edge_microdot.models import PointSample, PointTarget, utc_now_iso


def normalize_discovered_object_id(raw: Any) -> str | None:
    """Normalize a Who-Is / object-list entry to ``type,instance``."""
    if raw is None:
        return None
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        typ, inst = raw[0], raw[1]
        typ_s = str(typ).strip().replace("_", "-")
        inst_s = str(inst).strip()
        if typ_s and inst_s.isdigit():
            return f"{typ_s},{inst_s}"
        return None
    s = str(raw).strip().strip("()")
    if "," not in s:
        return None
    typ, inst = s.split(",", 1)
    typ_s = typ.strip().replace("_", "-")
    inst_s = inst.strip()
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


class Bacpypes3Transport:
    """Real BACpypes3 Application transport (only opened on start())."""

    def __init__(self, *, local_address: str = "0.0.0.0", device_instance: int = 999999) -> None:
        self.local_address = local_address
        self.device_instance = device_instance
        self.app: Any = None

    async def start(self) -> None:
        if self.app is not None:
            return
        from bacpypes3.app import Application
        from bacpypes3.local.device import DeviceObject
        from bacpypes3.pdu import Address

        dev = DeviceObject(
            objectIdentifier=("device", self.device_instance),
            objectName="live-ingest-client",
            vendorIdentifier=999,
            maxApduLengthAccepted=1476,
            segmentationSupported="segmentedBoth",
        )
        addr = Address(self.local_address)
        self.app = Application(dev, addr)

    async def close(self) -> None:
        if self.app is not None:
            self.app.close()
            self.app = None

    async def who_is(
        self,
        *,
        low_limit: int | None = None,
        high_limit: int | None = None,
        address: str | None = None,
    ) -> list[DeviceInfo]:
        await self.start()
        from bacpypes3.pdu import Address

        dest = Address(address) if address else None
        i_ams = await self.app.who_is(low_limit, high_limit, dest)
        out: list[DeviceInfo] = []
        for iam in i_ams:
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
        from bacpypes3.apdu import AbortPDU, ErrorPDU, RejectPDU
        from bacpypes3.pdu import Address
        from bacpypes3.primitivedata import ObjectIdentifier

        obj_id = ObjectIdentifier(object_identifier)
        try:
            return await self.app.read_property(
                Address(address),
                obj_id,
                property_identifier,
                array_index=array_index,
            )
        except (AbortPDU, RejectPDU, ErrorPDU) as exc:
            raise BacnetTransportError(str(exc), reason=str(getattr(exc, "apduAbortRejectReason", exc))) from exc

    async def read_property_multiple(
        self,
        address: str,
        items: Sequence[tuple[str, str]],
    ) -> list[tuple[str, str, int | None, Any]]:
        await self.start()
        from bacpypes3.apdu import AbortPDU, ErrorPDU, RejectPDU
        from bacpypes3.pdu import Address
        from bacpypes3.primitivedata import ObjectIdentifier

        spec: list[Any] = []
        for obj_id, prop_id in items:
            spec.append(ObjectIdentifier(obj_id))
            spec.append([prop_id])
        try:
            resp = await self.app.read_property_multiple(Address(address), spec)
        except (AbortPDU, RejectPDU, ErrorPDU) as exc:
            raise BacnetTransportError(str(exc), reason=str(getattr(exc, "apduAbortRejectReason", exc))) from exc

        rows: list[tuple[str, str, int | None, Any]] = []
        for obj_id, prop_id, arr_idx, val in resp:
            rows.append((str(obj_id), str(prop_id), arr_idx, val))
        return rows


class BacnetRpmSource:
    """Read-only BACnet polling & discovery source with adaptive RPM chunking."""

    def __init__(
        self,
        targets: Sequence[PointTarget],
        *,
        transport: BacnetTransport | None = None,
        local_address: str = "0.0.0.0",
        max_props: int = 25,
    ) -> None:
        self.targets = list(targets)
        self.transport: BacnetTransport = transport or Bacpypes3Transport(local_address=local_address)
        self.max_props = max(1, int(max_props))
        self.device_cache: dict[int, DeviceInfo] = {}
        for t in self.targets:
            inst = t.device_instance
            if inst is not None and t.device_address and inst not in self.device_cache:
                self.device_cache[inst] = DeviceInfo(device_instance=inst, address=t.device_address)

    async def close(self) -> None:
        await self.transport.close()

    async def who_is(
        self,
        *,
        low_limit: int | None = None,
        high_limit: int | None = None,
        address: str | None = None,
    ) -> list[DeviceInfo]:
        devices = await self.transport.who_is(
            low_limit=low_limit, high_limit=high_limit, address=address
        )
        for dev in devices:
            self.device_cache[dev.device_instance] = dev
        return devices

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
                resp = await self.transport.read_property_multiple(
                    address, [(t.object_id, t.property_id)]
                )
                if resp:
                    val, status = unwrap_bacnet_value(resp[0][3])
                    return [(t, val, status)]
            except Exception:
                pass
            # Single read_property fallback (covers devices that reject RPM entirely)
            try:
                raw = await self.transport.read_property(address, t.object_id, t.property_id)
                val, status = unwrap_bacnet_value(raw)
                return [(t, val, status)]
            except Exception as exc:
                return [(t, None, f"error:{exc}")]

        try:
            pairs = [(t.object_id, t.property_id) for t in batch]
            resp = await self.transport.read_property_multiple(address, pairs)
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
                        raw = await self.transport.read_property(address, t.object_id, t.property_id)
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
            raw = await self.transport.read_property(dev.address, dev_obj, "object-list")
            val, status = unwrap_bacnet_value(raw)
            if status == "ok" and isinstance(val, list):
                return [str(x) for x in val]
        except Exception as exc:
            if not _is_segmentation_abort(exc):
                raise

        length_raw = await self.transport.read_property(
            dev.address, dev_obj, "object-list", array_index=0
        )
        length_val, status = unwrap_bacnet_value(length_raw)
        if status != "ok" or not isinstance(length_val, int):
            raise BacnetTransportError(f"Unable to read object-list[0] length: {status}")

        objects: list[str] = []
        for idx in range(1, length_val + 1):
            item_raw = await self.transport.read_property(
                dev.address, dev_obj, "object-list", array_index=idx
            )
            item_val, item_status = unwrap_bacnet_value(item_raw)
            if item_status == "ok" and item_val is not None:
                objects.append(str(item_val))
        return objects

    async def discover_targets(
        self,
        device_instances: Sequence[int],
        *,
        site: str,
        skip_types: frozenset[str] = frozenset({"device", "network-port"}),
    ) -> list[PointTarget]:
        """Who-Is the requested devices, read each object-list, and build poll targets."""
        wanted = [int(x) for x in device_instances]
        if not wanted:
            self.targets = []
            return []
        try:
            await self.who_is()
        except Exception:
            # Targeted Who-Is below still runs per instance.
            pass
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
        self.targets = targets
        return targets
