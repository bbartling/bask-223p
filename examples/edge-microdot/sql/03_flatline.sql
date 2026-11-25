-- Sensor flatline: last N numeric samples have near-zero spread.
-- Parameters: window_size (INTEGER), epsilon (DOUBLE)
WITH ranked AS (
    SELECT
        ref,
        value_num,
        timestamp_utc,
        ROW_NUMBER() OVER (PARTITION BY ref ORDER BY timestamp_utc DESC) AS rn
    FROM telemetry
    WHERE value_num IS NOT NULL
),
windowed AS (
    SELECT ref, value_num, timestamp_utc
    FROM ranked
    WHERE rn <= CAST(? AS INTEGER)
),
agg AS (
    SELECT
        ref,
        MAX(value_num) - MIN(value_num) AS spread,
        COUNT(*) AS n,
        arg_max(value_num, timestamp_utc) AS last_val,
        MAX(timestamp_utc) AS ts_utc
    FROM windowed
    GROUP BY ref
)
SELECT
    'R-FLAT' AS rule_id,
    'sensor_flatline' AS rule_name,
    ref,
    'info' AS severity,
    'Flatline over ' || CAST(n AS VARCHAR) || ' samples (spread ' || CAST(spread AS VARCHAR) || ')' AS message,
    last_val AS value,
    strftime(ts_utc AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%gZ') AS observed_at_utc
FROM agg
WHERE n >= CAST(? AS INTEGER) AND spread <= ?;