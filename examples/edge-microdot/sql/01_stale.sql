-- Stale data: last sample older than stale window (SI timestamps, UTC).
-- Parameters: stale_minutes (INTEGER)
WITH latest AS (
    SELECT
        ref,
        MAX(timestamp_utc) AS ts_utc,
        arg_max(value_num, timestamp_utc) AS value_num,
        arg_max(units, timestamp_utc) AS units
    FROM telemetry
    GROUP BY ref
)
SELECT
    'R-STALE' AS rule_id,
    'stale_data' AS rule_name,
    ref,
    'warn' AS severity,
    'No sample within ' || CAST(? AS VARCHAR) || ' minutes' AS message,
    value_num AS value,
    strftime(ts_utc AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%gZ') AS observed_at_utc
FROM latest
WHERE ts_utc < (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - (CAST(? AS INTEGER) * INTERVAL '1' MINUTE);