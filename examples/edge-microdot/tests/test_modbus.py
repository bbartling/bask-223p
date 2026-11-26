"""Modbus decode, configured-point reads, and the async-only client."""
from __future__ import annotations

import asyncio
import struct
from pathlib import Path

from edge_microdot.sources.modbus_tcp import (
    ModbusPoint,
    ModbusTcpSource,
    decode_registers,
    load_modbus_points,
)


def test_decode_widths_orders_and_scale():
    assert decode_registers([0xFFFF], "int16") == -1
    assert decode_registers([0xFFFF], "uint16") == 65535
    assert decode_registers([0x0001, 0x0000], "uint32", word_order="little") == 1
    swapped = decode_registers([0x1234], "uint16", byte_order="little")
    assert swapped == 0x3412
    raw = struct.unpack(">HH", struct.pack(">f", 12.5))
    assert decode_registers(raw, "float32") == 12.5
    wide = struct.unpack(">HHHH", struct.pack(">d", 12.5))
    assert decode_registers(wide, "float64", byte_order="big", word_order="big") == 12.5
    assert decode_registers([100], "uint16", scale=0.1, offset=1) == 11.0


def test_reads_only_configured_points(tmp_path):
    path = tmp_path / "meter.yaml"
    path.write_text(
        """
modbus:
  - name: meter_energy
    host: 192.168.1.40
    unit_id: 2
    function: input
    address: 10
    data_type: uint16
    scale: 0.1
    offset: 1
""",
        encoding="utf-8",
    )
    points = load_modbus_points(path, site="lab")
    calls = []

    class _Fake:
        async def read_registers(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["function"] == "input"
            assert kwargs["unit_id"] == 2
            assert kwargs["address"] == 10
            assert kwargs["count"] == 1
            return [100]

        async def close(self):
            return None

    src = ModbusTcpSource(points, transport=_Fake(), gap_s=0, max_inflight=1)
    samples = asyncio.run(src.read_points(["meter_energy", "not-a-point"]))
    by_name = {sample.object: sample for sample in samples}
    assert by_name["not-a-point"].status == "error:not_configured"
    assert by_name["meter_energy"].value == 11.0
    assert len(calls) == 1
    again = asyncio.run(src.poll_once_async())
    assert [sample.object for sample in again] == ["meter_energy"]
    assert len(calls) == 2


def test_async_client_imports_when_extra_installed():
    pytest = __import__("pytest")
    pytest.importorskip("pymodbus")
    from pymodbus.client import AsyncModbusTcpClient

    assert AsyncModbusTcpClient.__name__ == "AsyncModbusTcpClient"


def test_client_is_async_pymodbus_only():
    text = Path(__file__).resolve().parents[1].joinpath(
        "edge_microdot", "sources", "modbus_tcp.py"
    ).read_text(encoding="utf-8")
    assert "from pymodbus.client import AsyncModbusTcpClient" in text
    assert text.count("ModbusTcpClient") == text.count("AsyncModbusTcpClient")
    assert "to_thread" not in text
    assert "threading" not in text
    assert "def write_" not in text


def test_one_request_at_a_time_per_device():
    point = ModbusPoint(name="a", host="10.0.0.5", unit_id=1, address=0, data_type="uint16")
    other = ModbusPoint(name="b", host="10.0.0.5", unit_id=1, address=1, data_type="uint16")
    state = {"n": 0, "max": 0}

    class _Slow:
        async def read_registers(self, **kwargs):
            state["n"] += 1
            state["max"] = max(state["max"], state["n"])
            await asyncio.sleep(0.02)
            state["n"] -= 1
            return [1]

        async def close(self):
            return None

    src = ModbusTcpSource([point, other], transport=_Slow(), gap_s=0, max_inflight=1)

    async def _go():
        await asyncio.gather(src.read_points(["a"]), src.read_points(["b"]))

    asyncio.run(_go())
    assert state["max"] == 1
