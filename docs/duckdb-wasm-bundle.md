# In-Memory DuckDB + Portable Parquet FDD Bundle + Browser DuckDB-WASM

Generic architecture note for edge and gateway FDD deployments (such as Normal Framework Open-FDD releases and lightweight Microdot edge nodes).

## Goals

1. **Zero growing database file on flash/SD storage:** run DuckDB strictly in memory (`duckdb.connect(":memory:")`) with bounded RAM (`memory_limit`, rolling time/row retention).
2. **Infrequent, crash-safe persistence:** periodically export a compact ZSTD-compressed Parquet snapshot (`COPY ... TO tmp (FORMAT PARQUET, COMPRESSION ZSTD)` + atomic rename), rotate/prune oldest snapshots under a configurable byte cap (100 MB default), and reload the newest valid snapshot into `:memory:` on startup.
3. **Zero-compute browser analytics (`/sql`):** export a standardized Parquet ZIP bundle over an authenticated endpoint and run DuckDB-WASM in the operator's browser so ad-hoc SQL and FDD rule tuning never load the host CPU/RAM.

## Process model

DuckDB stays **in-process** with the edge (or gateway) application. There is no required sidecar: ingest, snapshots, and scheduled SQL rules share one `:memory:` database.

An optional later **fdd-worker** container may share the Parquet snapshot volume (`var/snapshots/`) with the ingest container if rule evaluation needs to be isolated. That worker is out of scope for the current edge app; the on-disk Parquet contract (`sample_store` / `point` / `rule_result`, and the portable bundle below) is the interface it would read.

## Portable Bundle Contract

Authenticated endpoint: `GET /api/export/bundle?hours=N` (Bearer token auth; default `hours=24`).

Returns `application/zip` containing three Parquet files whose schemas match the Open-FDD core / input-contract tables (`sql/fdd/schema/core.sql`, `sql/fdd/input_contract.json`). Values are SI; timestamps are UTC. `fdd.point.role` is the 223P-derived snake_case input term (open-fdd#1804), not an Open-FDD acronym.

1. **`input_sample.parquet`** — `fdd.input_sample`
   - `site_id` (`VARCHAR`)
   - `point_key` (`VARCHAR`)
   - `ts` (`TIMESTAMPTZ`, UTC)
   - `value` (`DOUBLE`, SI)
2. **`point.parquet`** — `fdd.point`
   - `site_id`, `point_key`, `equipment_id`, `role` (223P snake_case input term), `role_source`, `kind`, `unit` (SI), `source_unit`, `si_scale`, `si_offset`, `haystack_id`, `attrs` (JSON), `updated_at`
   - Issue #1804 aliases are views, not extra files: `point_id = point_key`, `equipment = equipment_id`, `input_term = role`
3. **`rule_result.parquet`** — `fdd.rule_result`
   - `result_id`, `site_id`, `equipment_id`, `rule_id`, `subject_key`, `window_start`, `window_end`, `rule_hash`, `config_hash`, `config_revision`, `effective_parameters`, `poll_seconds`, `status` (`PASS`/`FAULT`/`INCONCLUSIVE`/`SKIPPED`/`ERROR`/`STALE`/`METRIC`), `reason`, `fault_hours`, `covered_hours`, `fault_pct`, `role`, `engine`, `metrics`, `diagnostics`, `source_watermark`, `created_at`

See [parity-with-open-fdd-nf.md](parity-with-open-fdd-nf.md) for a table of matches and deliberate differences.

## Scheduled FDD & Cookbook Rules Compatibility

- Scheduled FDD rules (`sql/fdd/rules/*.sql`, ids `SV-STALE`, `SV-RANGE`, `SV-FLATLINE`, `SV-SPIKE`) run on the host against `sensor_history` (join of `fdd.input_sample` and `fdd.point`) and upsert rows into `fdd.rule_result`.
- In the browser `/sql` runner, after registering the three Parquet files, create `fdd.input_sample`, `fdd.point`, `fdd.rule_result`, and the `sensor_history` view. The same cookbook files then execute **unchanged** inside DuckDB-WASM.

## Browser DuckDB-WASM Runner (`/sql`)

- Protected by session login; fetches an access token and calls `GET /api/export/bundle?hours=N` and `GET /api/sql-rules`.
- Loads DuckDB-WASM from jsDelivr CDN by default, with environment overrides (`EDGE_DUCKDB_WASM_URL`, `EDGE_DUCKDB_WASM_BUNDLE_BASE`) to serve local/air-gapped static assets.
- Unzips the bundle in browser memory (standard `DEFLATE` via `DecompressionStream('deflate-raw')` or `STORED`), registers the Parquet buffers as the Open-FDD `fdd.*` views, populates the `sql/fdd/rules/*.sql` dropdown with default parameters, and lets operators edit, execute, and inspect results locally.
