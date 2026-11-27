# DuckDB FDD cookbook (Open-FDD layout)

Rule files live in `sql/fdd/rules/`, matching the Open-FDD convention
`sql/fdd/postgres/rules/<rule>.sql`. Filenames and rule ids follow the
sensor-sweep catalog: `sv_stale.sql` (`SV-STALE`), `sv_range.sql`
(`SV-RANGE`), `sv_flatline.sql` (`SV-FLATLINE`), `sv_spike.sql` (`SV-SPIKE`).

Rules read the Open-FDD input contract:

- `fdd.input_sample(site_id, point_key, ts, value)` — long SI samples, UTC
- `fdd.point` — bound points; `role` holds the 223P-derived snake_case input term
  (see [open-fdd#1804](https://github.com/bbartling/open-fdd/issues/1804); no
  acronyms such as `sat` or `mat`)
- `sensor_history` — convenience view joining those two
  (`equipment_id`, `series_id`, `sensor_role`, `timestamp_utc`, `value`, `unit`)

Each rule returns the Open-FDD sweep output columns:
`equipment_id`, `series_id`, `sensor_role`, `fault_hours`, `covered_hours`.

The host runner upserts those rows into `fdd.rule_result`. The same files run
on the edge through `POST /api/sql` against the in-memory DuckDB tables
`fdd.input_sample`, `fdd.point`, and `fdd.rule_result`. Parquet snapshots are
the on-disk history. There is no browser database.

These are simplified in-process DuckDB implementations of the sensor-sweep
rules. They share names, input/output columns, and SI units with the Open-FDD
NF pack, but they do not vendor the PostgreSQL pack helpers, run-proof gates,
or `{{PLACEHOLDER}}` rendering.
