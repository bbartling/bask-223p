# BACnet driver

The Worker Pi 1 profile binds BACpypes3 to the Pi's own LAN address
(`EDGE_BACNET_ADDRESS`, for example `192.168.1.50/24`). A wildcard such as
`0.0.0.0` is rejected. Route-aware mode is on, so an I-Am source like
`2000:7@192.168.204.200` stays intact. UDP 47808 has to be free on that
address before the process starts.

The client and the HTTP server share one asyncio loop. Do not open a second
BACpypes3 application on another loop.

## What is polled

Do not map or poll every point on a live system. Legacy MS/TP and small IoT
networks bottleneck easily, and a full object-list poll will stall them.
Polling reads **configured points only** (`--points`). Naming a device with
`--devices` or `EDGE_BACNET_DEVICES` does not copy its object-list into the
poll set. A controller with thousands of objects is not walked on a timer.

Object-list discovery runs when a caller names **one** device
(`GET /api/bacnet/devices/<instance>/objects` or `--scan`). The catalog
returns object-identifier, object-type, object-name, units, and description.
It does not read `present-value`. Missing properties (units on a binary
object, for example) stay on that property.

Who-Is is range-limited: `low` and `high` are both required. A scan of
device instances 5007 and 54545 sends a Who-Is for each instance
(`low == high`), then reads that device only.

## Pace

MS/TP and small IP controllers drop bursts. Each device has:

| Variable | Default | Meaning |
| --- | --- | --- |
| `EDGE_BACNET_MAX_INFLIGHT` | `1` | Requests in flight to one device |
| `EDGE_BACNET_CHUNK_DELAY_S` | `0.05` | Gap between request starts. Raise it (for example `0.2`) on MS/TP |
| `EDGE_BACNET_WHOIS_TIMEOUT` | `5` | Seconds to collect I-Am replies |

ReadPropertyMultiple chunks shrink to the device's `max-apdu-length-accepted`
and segmentation support (25 properties at the top, fewer when the APDU is
206, 480, or 1024 bytes, and fewer again when segmentation is off). A chunk
that aborts is split in half. One property falls back to ReadProperty. A
timeout is retried once. A per-property error does not fail the rest of the
chunk. `object-list` and `priority-array` fall back to array index 0 (or
1..16) when the whole array cannot be segmented.

## Reads

Token auth matches the other `/api` routes (`Authorization: Bearer`).

| Method | Path | Body / query |
| --- | --- | --- |
| GET | `/api/bacnet/whois?low=&high=&timeout=&address=` | Range-limited Who-Is. `address` may be `2000:*@192.168.204.200` |
| GET | `/api/bacnet/devices` | Who-Is for each named `--devices` instance, plus one `object-name` read |
| GET | `/api/bacnet/devices/<instance>` | Device-object properties, not the object-list |
| GET | `/api/bacnet/devices/<instance>/objects` | One device's catalog |
| POST | `/api/bacnet/read` | `{"device":5007,"reads":[{"object":"analog-input,1","property":"present-value"}]}` |
| GET | `/api/bacnet/priority?device=&object=` | 16 priority slots and `relinquish-default`. Each slot is a number, a bool, or null |

`GET /api/bacnet/whois` leaves `object_name` blank. An I-Am does not carry it,
and filling it for every device in a range would be another fan-out.
`GET /api/bacnet/devices` only talks to the named instances, so it follows
each I-Am with one `object-name` read.

`POST /api/bacnet/read` accepts at most `EDGE_BACNET_READ_MAX` entries in
`reads` (default 50) and returns 400 above that. Who-Is, the one-device
catalog, priority-array, and this read route are also paced by
`EDGE_BACNET_MAX_INFLIGHT` and `EDGE_BACNET_CHUNK_DELAY_S`. Do not raise the
cap to walk a whole controller.

Discovery does not assign 223P point roles. Configured points keep the role
names already in `edge_microdot.role_contract` (the open-fdd vocabulary, for
example `discharge_air_temperature`).

## Writes

Off unless `EDGE_BACNET_WRITES_ENABLED=1`. The caller also needs `EDGE_ROLES`
to map that user to `admin` (`viewer` and `operator` cannot write). Priority
defaults to 8 and must be 1..16. Priorities 1 and 2 need
`EDGE_BACNET_ALLOW_PRIORITY_1_2=1`. Release is a NULL write at a priority
(`POST /api/bacnet/release`). Every write and release, including a denial, is
appended to `EDGE_BACNET_AUDIT_LOG` (default `var/bacnet-audit.log`).

`EDGE_BACNET_BBMD` optionally registers this host as a foreign device. Leave
it unset on the same subnet as the controllers.

## Read-only scan on the Pi

Stop anything else bound to UDP 47808, then:

```bash
export EDGE_DRIVER=bacnet
export EDGE_BACNET_ADDRESS=192.168.1.50/24
python -m edge_microdot --driver bacnet \
  --address "$EDGE_BACNET_ADDRESS" \
  --scan --devices 5007,54545
```

That command does not set `EDGE_BACNET_WRITES_ENABLED` and does not read
present-value. Each instance gets its own Who-Is.
