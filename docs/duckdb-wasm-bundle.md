# In-Memory DuckDB + Portable Parquet FDD Bundle + Browser DuckDB-WASM

Generic architecture note for edge and gateway FDD deployments (such as Normal Framework Open-FDD releases and lightweight Microdot edge nodes).

## Goals

1. **Zero growing database file on flash/SD storage:** run DuckDB strictly in memory (`duckdb.connect(":memory:")`) with bounded RAM (`memory_limit`, rolling time/row retention).
2. **Infrequent, crash-safe persistence:** periodically export a compact ZSTD-compressed Parquet snapshot (`COPY ... TO tmp (FORMAT PARQUET, COMPRESSION ZSTD)` + atomic rename), rotate/prune oldest snapshots under a configurable byte cap (100 MB default), and reload the newest valid snapshot into `:memory:` on startup.
3. **Zero-compute browser analytics (`/sql`):** export a standardized Parquet ZIP bundle over an authenticated endpoint and run DuckDB-WASM in the operator's browser so ad-hoc SQL and FDD rule tuning never load the host CPU/RAM.

## Portable Bundle Contract

Authenticated endpoint: `GET /api/export/bundle?hours=N` (Bearer token auth; default `hours=24`).

Returns `application/zip` containing three Parquet files with generic, tool-neutral schemas:

1. **`samples.parquet`**
   - `ts` (`TIMESTAMPTZ`, UTC)
   - `point_id` (`VARCHAR`, canonical point identifier)
   - `value` (`DOUBLE`, converted to SI units)
   - `units` (`VARCHAR`, SI unit symbol: `degC`, `K`, `Pa`, `m3/s`, `W`, `J`, `Hz`, `%`, `%RH`, `ppm`, `B`)
2. **`fdd_results.parquet`**
   - `ts` (`TIMESTAMPTZ`, UTC)
   - `equipment` (`VARCHAR`)
   - `rule` (`VARCHAR`)
   - `fault_flag` (`BOOLEAN`)
   - `detail` (`VARCHAR`)
3. **`points.parquet`**
   - `point_id` (`VARCHAR`)
   - `equipment` (`VARCHAR`)
   - `haystack_tags` (`VARCHAR`)
   - `class_223p` (`VARCHAR`)
   - `unit` (`VARCHAR`, SI unit symbol)

## Scheduled FDD & Cookbook Rules Compatibility

- Scheduled FDD rules (`sql/*.sql`) run on the host against the in-memory DuckDB `telemetry` table and record faults into `fdd_results`.
- In the browser `/sql` runner, after registering the three Parquet files (`samples`, `fdd_results`, `points`), create a `telemetry` view mapping `(point_id -> ref, ts -> timestamp_utc, value -> value_num, CAST(value AS VARCHAR) -> value_text, units -> units)`.
- With that view in place, the exact same `sql/*.sql` cookbook rules execute **unchanged** inside DuckDB-WASM.

## Browser DuckDB-WASM Runner (`/sql`)

- Protected by session login; fetches an access token and calls `GET /api/export/bundle?hours=N` and `GET /api/sql-rules`.
- Loads DuckDB-WASM from jsDelivr CDN by default, with environment overrides (`EDGE_DUCKDB_WASM_URL`, `EDGE_DUCKDB_WASM_BUNDLE_BASE`) to serve local/air-gapped static assets.
- Unzips the bundle in browser memory (standard `DEFLATE` via `DecompressionStream('deflate-raw')` or `STORED`), registers the Parquet buffers, populates the `sql/*.sql` dropdown with default parameters, and lets operators edit, execute, and inspect results locally.
