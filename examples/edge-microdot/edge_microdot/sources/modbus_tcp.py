"""Read-only Modbus TCP on the edge asyncio loop.

Uses pymodbus ``AsyncModbusTcpClient`` only. There is no sync client and no
worker thread. Writes are not implemented. Polling reads configured points
only; the client never scans a device's address space.

Word order is the order of 16-bit registers. Byte order swaps the two bytes
inside each register before the registers are joined. ``big`` / ``big`` is
the usual Modbus layout.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

import yaml

from edge_microdot.models import PointSample, utc_now_iso
from edge_microdot.sources.bacnet_rpm import DevicePacer

_WIDTH = {
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "float64": 4,
}
_STRUCT = {
    "int16": ">h",
    "uint16": ">H",
    "int32": ">i",
    "uint32": ">I",
    "float32": ">f",
    "float64": ">d",
}


class ModbusReadError(RuntimeError):
    """A configured register read failed."""


class ModbusTransport(Protocol):
    async def read_registers(
        self,
        *,
        host: str,
        port: int,
        unit_id: int,
        function: str,
        address: int,
        count: int,
        timeout: float,
    ) -> list[int]: ...

    async def close(self) -> None: ...


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


def ordered_bytes(words: Sequence[int], *, byte_order: str, word_order: str) -> bytes:
    regs = [int(word) & 0xFFFF for word in words]
    if byte_order == "little":
        regs = [((word & 0xFF) << 8) | ((word >> 8) & 0xFF) for word in regs]
    elif byte_order != "big":
        raise ValueError("byte_order must be big or little")
    if word_order == "little":
        regs.reverse()
    elif word_order != "big":
        raise ValueError("word_order must be big or little")
    return b"".join(struct.pack(">H", word) for word in regs)


def decode_registers(
    words: Sequence[int],
    data_type: str,
    *,
    byte_order: str = "big",
    word_order: str = "big",
    scale: float = 1.0,
    offset: float = 0.0,
) -> int | float:
    kind = data_type.strip().lower()
    if kind not in _WIDTH:
        raise ValueError(f"unsupported data_type {data_type}")
    need = _WIDTH[kind]
    if len(words) < need:
        raise ValueError(f"{kind} needs {need} registers")
    raw = ordered_bytes(words[:need], byte_order=byte_order, word_order=word_order)
    value = struct.unpack(_STRUCT[kind], raw)[0]
    if scale == 1 and offset == 0 and kind in {"int16", "uint16", "int32", "uint32"}:
        return int(value)
    return float(value) * float(scale) + float(offset)


@dataclass(slots=True)
class ModbusPoint:
    name: str
    host: str
    port: int = 502
    unit_id: int = 1
    function: str = "holding"
    address: int = 0
    data_type: str = "uint16"
    byte_order: str = "big"
    word_order: str = "big"
    scale: float = 1.0
    offset: float = 0.0
    timeout_s: float = 3.0
    site: str = "demo-site"

    def key(self) -> str:
        return f"{self.host}:{self.port}:{self.unit_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "unit_id": self.unit_id,
            "function": self.function,
            "address": self.address,
            "data_type": self.data_type,
            "byte_order": self.byte_order,
            "word_order": self.word_order,
            "scale": self.scale,
            "offset": self.offset,
            "timeout_s": self.timeout_s,
            "site": self.site,
        }


def load_modbus_points(path: str | Path, *, site: str = "demo-site") -> list[ModbusPoint]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rows = raw.get("modbus") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    default_site = str(raw.get("site") or site) if isinstance(raw, dict) else site
    points: list[ModbusPoint] = []
    for item in rows:
        if not isinstance(item, dict) or not item.get("name") or not item.get("host"):
            raise ValueError("each modbus point needs name and host")
        function = str(item.get("function") or "holding").strip().lower()
        if function not in {"holding", "input"}:
            raise ValueError("function must be holding or input")
        data_type = str(item.get("data_type") or "uint16").strip().lower()
        if data_type not in _WIDTH:
            raise ValueError(f"unsupported data_type {data_type}")
        points.append(
            ModbusPoint(
                name=str(item["name"]),
                host=str(item["host"]),
                port=int(item.get("port") or 502),
                unit_id=int(item.get("unit_id") if item.get("unit_id") is not None else 1),
                function=function,
                address=int(item.get("address") or 0),
                data_type=data_type,
                byte_order=str(item.get("byte_order") or "big").strip().lower(),
                word_order=str(item.get("word_order") or "big").strip().lower(),
                scale=float(item.get("scale") if item.get("scale") is not None else 1),
                offset=float(item.get("offset") if item.get("offset") is not None else 0),
                timeout_s=float(item.get("timeout_s") or item.get("timeout") or 3),
                site=str(item.get("site") or default_site),
            )
        )
    return points


class PymodbusAsyncTransport:
    """One AsyncModbusTcpClient per host/port/timeout, awaited on the caller loop."""

    def __init__(self) -> None:
        self._clients: dict[tuple[str, int, float], Any] = {}

    async def _client(self, host: str, port: int, timeout: float) -> Any:
        try:
            from pymodbus.client import AsyncModbusTcpClient
        except ImportError as exc:
            raise ModbusReadError(
                "pymodbus is not installed. Install the modbus extra: pip install 'edge-microdot[modbus]'"
            ) from exc
        key = (host, int(port), float(timeout))
        client = self._clients.get(key)
        if client is None:
            client = AsyncModbusTcpClient(
                host,
                port=int(port),
                timeout=float(timeout),
                retries=1,
                reconnect_delay=0,
            )
            self._clients[key] = client
        if not client.connected:
            ok = await client.connect()
            if not ok:
                raise ModbusReadError(f"connect failed {host}:{port}")
        return client

    async def read_registers(
        self,
        *,
        host: str,
        port: int,
        unit_id: int,
        function: str,
        address: int,
        count: int,
        timeout: float,
    ) -> list[int]:
        client = await self._client(host, port, timeout)
        if function == "holding":
            result = await client.read_holding_registers(int(address), count=int(count), device_id=int(unit_id))
        elif function == "input":
            result = await client.read_input_registers(int(address), count=int(count), device_id=int(unit_id))
        else:
            raise ModbusReadError("function must be holding or input")
        if getattr(result, "isError", lambda: False)():
            raise ModbusReadError(str(result))
        return [int(word) & 0xFFFF for word in result.registers]

    async def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()


class ModbusTcpSource:
    """Poll configured holding and input registers. Nothing else."""

    def __init__(
        self,
        points: Sequence[ModbusPoint],
        *,
        transport: ModbusTransport | None = None,
        max_inflight: int | None = None,
        gap_s: float | None = None,
    ) -> None:
        self.points = list(points)
        self.transport: ModbusTransport = transport or PymodbusAsyncTransport()
        inflight = max_inflight if max_inflight is not None else _env_int("EDGE_MODBUS_MAX_INFLIGHT", 1)
        gap = gap_s if gap_s is not None else _env_float("EDGE_MODBUS_GAP_S", 0.05)
        self.pacer = DevicePacer(max_inflight=inflight, gap_s=gap)

    async def close(self) -> None:
        await self.transport.close()

    def _select(self, names: Sequence[str] | None) -> tuple[list[ModbusPoint], list[str]]:
        if not names:
            return list(self.points), []
        by_name = {point.name: point for point in self.points}
        chosen: list[ModbusPoint] = []
        missing: list[str] = []
        for name in names:
            point = by_name.get(name)
            if point is None:
                missing.append(name)
            else:
                chosen.append(point)
        return chosen, missing

    async def read_points(self, names: Sequence[str] | None = None) -> list[PointSample]:
        chosen, missing = self._select(names)
        ts = utc_now_iso()
        samples: list[PointSample] = []
        for name in missing:
            samples.append(
                PointSample(
                    timestamp_utc=ts,
                    site="",
                    source="modbus-tcp",
                    device="",
                    object=name,
                    property="",
                    value=None,
                    status="error:not_configured",
                )
            )
        for point in chosen:
            need = _WIDTH[point.data_type]
            try:
                async with self.pacer.slot(point.key()):
                    words = await self.transport.read_registers(
                        host=point.host,
                        port=point.port,
                        unit_id=point.unit_id,
                        function=point.function,
                        address=point.address,
                        count=need,
                        timeout=point.timeout_s,
                    )
                value = decode_registers(
                    words,
                    point.data_type,
                    byte_order=point.byte_order,
                    word_order=point.word_order,
                    scale=point.scale,
                    offset=point.offset,
                )
                status = "ok"
            except Exception as exc:
                value = None
                status = f"error:{exc}"
            samples.append(
                PointSample(
                    timestamp_utc=ts,
                    site=point.site,
                    source="modbus-tcp",
                    device=point.key(),
                    object=point.name,
                    property=point.data_type,
                    value=value,
                    status=status,
                )
            )
        return samples

    async def poll_once_async(self) -> list[PointSample]:
        return await self.read_points(None)

    def poll_once(self) -> list[PointSample]:
        import asyncio

        return asyncio.run(self.poll_once_async())
