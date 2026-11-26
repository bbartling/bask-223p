# DuckDB sweep SQL

Plain read-only rule queries against the `telemetry` table (on the edge host) or the `telemetry` compatibility view registered in browser DuckDB-WASM at `/sql`.

You can run these directly in `/sql` with zero server-side compute, or on a PC against an exported `samples.parquet` bundle:

```sql
CREATE VIEW telemetry AS
SELECT point_id AS ref, ts AS timestamp_utc, value AS value_num,
       CAST(value AS VARCHAR) AS value_text, units
FROM read_parquet('samples.parquet');
```

Open each `.sql` file and bind the parameters documented in the header comments.
