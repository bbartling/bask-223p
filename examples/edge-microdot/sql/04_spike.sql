-- Spike / jump: consecutive sample delta exceeds threshold (generic SI step).
-- Parameters: jump_threshold (DOUBLE)
WITH ordered AS (
    SELECT
        ref,
        value_num,
        timestamp_utc,
        LAG(value_num) OVER (PARTITION BY ref ORDER BY timestamp_utc) AS prev_val
    FROM telemetry
    WHERE value_num IS NOT NULL
),
hits AS (
    SELECT *
    FROM ordered
    WHERE prev_val IS NOT NULL AND abs(value_num - prev_val) > ?
)
SELECT
    'R-SPIKE' AS rule_id,
    'spike_jump' AS rule_name,
    ref,
    'warn' AS severity,
    'Step change ' || CAST(abs(value_num - prev_val) AS VARCHAR) || ' > ' || CAST(? AS VARCHAR) AS message,
    value_num AS value,
    strftime(timestamp_utc AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%gZ') AS observed_at_utc
FROM hits
QUALIFY ROW_NUMBER() OVER (PARTITION BY ref ORDER BY timestamp_utc DESC) = 1;