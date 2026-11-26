# live-ingest (Unified Phase-2 Live Ingest & SPARQL/DuckDB Service)

A single-codebase, strictly read-only FastAPI service and CLI showing how data-lake and FDD integrators move from an ASHRAE 223P / Brick Turtle graph (`export-223p` or a BACpypes3 `bacnet-rdf` scan) to live BACnet / Niagara telemetry stored locally in **DuckDB** and **CSV**.

**Strict read-only posture:** neither driver exposes or sends BACnet writes (`WriteProperty`) or baskStream mutation operations, and both SPARQL and DuckDB SQL endpoints reject mutating queries.

## Design choices (Python 3.14 stack)

* **RDF query path — `pyoxigraph` + `rdflib`. 223P checks call `validate_223p`:**
  * **`pyoxigraph==0.5.13`** (Rust-backed Oxigraph SPARQL 1.1 store) executes `SELECT` / `ASK` / `CONSTRUCT` / `DESCRIBE` queries natively without Python GIL overhead on property paths and joins.
  * **`rdflib==7.6.0`** parses Turtle. Validation is `ontoenv==0.6.4` plus `pyshifty==0.6.0` through `validate_223p`. There is no pySHACL fallback. `pyshacl==0.40.2` is only the opt-in `--cross-check pyshacl`.
* **Local time-series store — `DuckDbSink` (`duckdb==2.0.1`) + `CsvSink` + `StdoutSink`:**
  * `telemetry` table keyed by `ref` (`bacnet://<device>/<type>,<instance>`), `timestamp_utc` (`TIMESTAMPTZ`), `site`, `source`, `device`, `object`, `property`, `value_num`, `value_text`, `units`, `status`.
  * `CsvSink` writes `timestamp_utc,site,source,device,object,property,value,units,status` cleanly alongside DuckDB.
* **Two source drivers behind one CLI/API (`--source baskstream | bacnet | both`):**
  * **`BaskStreamSource`**: uses `bask_223p.bacnet.baskstream.ReadOnlyBaskStreamClient` over authenticated WebSocket + MessagePack (`read` polling and `subscribe` / `recv_event` COV).
  * **`BacnetRpmSource`**: uses `bacpypes3==0.1.0` ReadPropertyMultiple with APDU-budget-aware chunking (`max_props=25`, shrunk for small `max-apdu-length-accepted` / non-segmenting devices), recursive batch splitting on `AbortPDU`/`RejectPDU`/`ErrorPDU`, single `read_property` fallback, per-property `ErrorType` preservation, Who-Is device metadata, and `object-list` `segmentationNotSupported` array-index (`0`, `1..N`) discovery fallback.
* **Unified FastAPI UI & routes:**
  * `GET /playground` (HTML SPARQL + DuckDB SQL + `bacnet://` Join UI) and `GET /docs` (Swagger)
  * `GET /health`, `GET /points`, `POST /poll/once`, `GET /latest`, `GET /history?ref=...&start=...&end=...`, `WebSocket /live`
  * `POST /api/sparql`, `POST /api/sparql/upload`, `GET /api/sparql/examples`, `GET /api/model/ttl`, `POST /api/model/validate`
  * `POST /api/sql` (read-only DuckDB SQL playground), `POST /api/join` & `GET /api/join/example` (SPARQL-to-`bacnet://`-ref-to-DuckDB history join)
  * Read-only BACnet discovery (`--source bacnet` or `both`): `GET /bacnet/who-is`, `GET /bacnet/devices/{device_id}/objects`, `POST /bacnet/read-multiple`

## Quick start (Python 3.14)

```powershell
cd examples/live-ingest
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -e ../..
.\.venv\Scripts\python.exe -m pytest -q
```

### Run the FastAPI Playground + DuckDB + SPARQL

```powershell
python -m live_ingest --source baskstream --station https://localhost --insecure `
  --env-file path/to/niagara_station.env --points ../../outputs/station/site-223p.ttl `
  --duckdb var/live_ingest.duckdb --csv var/live_ingest.csv --serve --port 8080
# Playground: http://127.0.0.1:8080/playground
# Swagger:    http://127.0.0.1:8080/docs
```

### Docker Compose (two profiles: `niagara` and `bacnet`)

```powershell
# Niagara / baskStream profile
docker compose --profile niagara up --build

# Direct BACnet/IP profile (host networking on Linux for UDP 47808)
docker compose --profile bacnet up --build
```

> **UDP 47808 note:** If a Niagara station or BACnet service already binds UDP port `47808` on the same host, BACpypes3 cannot share `47808` on that interface. Run `--source bacnet` (or the `bacnet` Docker profile) on another host/VM or stop the local station first, or use `--source baskstream`.

### SPARQL → `bacnet://` Ref → DuckDB History Join & Open-FDD Notebook

* Script: `python scripts/sparql_to_duckdb_join.py`
* Prototype notebook: `notebooks/openfdd_duckdb_faults.ipynb` (Open-FDD FC1 SQL rule over DuckDB `telemetry`)
