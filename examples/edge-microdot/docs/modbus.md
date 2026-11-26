# Modbus TCP

Occasional utility meters and solar inverters. Reads are holding registers
(function 3) and input registers (function 4) for **configured points only**.
The driver does not scan a device. There is no write path.

The client is pymodbus `AsyncModbusTcpClient` (3.x), awaited on the same
asyncio loop as Microdot and BACnet. The sync `ModbusTcpClient` is not used,
and reads are not moved onto a thread. Install the extra when you need it:

```bash
python -m pip install -e '.[modbus]'
```

The BACnet image does not install pymodbus.

## Point file

```yaml
site: demo-site
modbus:
  - name: meter_energy
    host: 192.168.1.40
    port: 502
    unit_id: 1
    function: holding
    address: 100
    data_type: float32
    byte_order: big
    word_order: big
    scale: 0.1
    offset: 0
    timeout_s: 3
```

`data_type` is `int16`, `uint16`, `int32`, `uint32`, `float32`, or `float64`.
`byte_order` swaps bytes inside each 16-bit register. `word_order` reverses
register order. `big` / `big` is standard Modbus. The stored value is
`decoded * scale + offset`.

`EDGE_MODBUS_MAX_INFLIGHT` (default 1) and `EDGE_MODBUS_GAP_S` (default
`0.05`) pace each `host:port:unit_id`. pymodbus retries a request once
(`retries=1`) and does not keep a reconnect loop (`reconnect_delay=0`).

## API

Bearer token, same as the other `/api` routes.

| Method | Path | Meaning |
| --- | --- | --- |
| GET | `/api/modbus/points` | The configured points. No register read |
| POST | `/api/modbus/read` | `{"names":["meter_energy"]}` or omit `names` to read every configured point |

A name that is not in the file returns `error:not_configured` and does not
open a socket. The poll loop uses the same configured list.

```bash
export EDGE_DRIVER=modbus
python -m edge_microdot --driver modbus --points configs/meter.yaml
```
