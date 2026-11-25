-- Out of range for common SI-ish units (°C default band).
-- Parameters: temp_lo, temp_hi (DOUBLE)
WITH latest AS (
    SELECT
        ref,
        arg_max(value_num, timestamp_utc) AS value_num,
        arg_max(units, timestamp_utc) AS units,
        MAX(timestamp_utc) AS ts_utc
    FROM telemetry
    WHERE value_num IS NOT NULL
    GROUP BY ref
),
scored AS (
    SELECT
        *,
        CASE
            WHEN upper(replace(units, '°', 'DEG_')) IN ('C', 'CEL', 'DEG_C') THEN value_num
            ELSE NULL
        END AS si_value
    FROM latest
)
SELECT
    'R-RANGE' AS rule_id,
    'out_of_range' AS rule_name,
    ref,
    'alarm' AS severity,
    'Value ' || CAST(si_value AS VARCHAR) || ' outside [' || CAST(? AS VARCHAR) || ',' || CAST(? AS VARCHAR) || '] °C' AS message,
    si_value AS value,
    strftime(ts_utc AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%gZ') AS observed_at_utc
FROM scored
WHERE si_value IS NOT NULL AND (si_value < ? OR si_value > ?);