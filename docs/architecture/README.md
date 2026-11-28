# Architecture

Three pictures, drawn as SVG. The Mermaid files record the same structure. They are not the picture source.

- [Test bench](test-bench.svg) ([source](test-bench.mmd))
- [Campus](campus.svg) ([source](campus.mmd))
- [Central collector](central.svg) ([source](central.mmd))

Rendered copies: [test-bench.png](test-bench.png), [campus.png](campus.png), [central.png](central.png).

## What the bench code actually runs

Each Pi runs one Compose profile of `examples/edge-microdot`. The profiles do not share a process.

**Worker Pi 1** (`docker compose --profile bacnet`) runs the BACpypes3 driver on the host network. The routes that are implemented, and covered by `tests/test_bacnet_api.py` and `tests/test_bacnet_rpm.py`, are Who-Is, a one-device object catalog, ReadProperty, ReadPropertyMultiple (`POST /api/bacnet/read` with a `reads` list, capped at 50), and the priority array. WriteProperty and release exist and return 403 unless `EDGE_BACNET_WRITES_ENABLED` is set and the caller is `admin`. Compose defaults name device instances 54545 and 5007 for Who-Is. That list is not an object-list poll. Present values are read only for rows in `EDGE_POINTS_FILE`.

**Worker Pi 2** (`docker compose --profile niagara`) runs the baskStream client: authenticated WebSocket, read-only, SCRAM with an HTTP Basic fallback. It polls the points file. An empty file does not browse the station into the poll set. There is no BACnet write path on this profile.

Both profiles keep history in an in-memory DuckDB database and write Parquet snapshots under `var/snapshots/`. `/sql` runs read-only SQL on that database. SPARQL is on unless `EDGE_ENABLE_SPARQL=0`. Each Pi builds the 223P graph on the device (QUDT local-name units, `bacnet://` refs, Haystack only as an overlay) and serves it at `GET /api/model.ttl` and `GET /api/export.zip`.

The central collector is a third process, `python -m central`. On a schedule it pulls `GET /api/export.zip` from each Pi, stores one named graph per edge, and runs reconcile there. It does not run on the Pi, and it does not open a socket to the FEC, the BASrouter, or the station. See [central/README.md](../../central/README.md).

The local SQL rules under `sql/fdd/rules/` are a small sensor sweep on the edge. The model export is what an Open-FDD rules bundle consumes. The 5.0.22 roles file is not vendored yet.
