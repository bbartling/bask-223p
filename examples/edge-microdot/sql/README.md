# DuckDB sweep SQL

Plain read-only rule queries against the `telemetry` table. Run on a Pi via the app timer, or on a PC with the DuckDB CLI/UI against a **copy** of `edge_history.duckdb`:

```bash
duckdb -ui /path/to/edge_history.duckdb
```

Open each `.sql` file and bind the parameters documented in the header comments.