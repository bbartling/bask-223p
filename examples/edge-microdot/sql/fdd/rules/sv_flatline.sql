-- SV-FLATLINE: spread of samples in the last FLATLINE_HOURS is at most FLATLINE_TOL (SI).
-- Parameters: flatline_hours (DOUBLE), flatline_tol (DOUBLE)
-- Output: equipment_id, series_id, sensor_role, fault_hours, covered_hours
WITH windowed AS (
    SELECT
        equipment_id,
        series_id,
        sensor_role,
        value,
        timestamp_utc
    FROM sensor_history
    WHERE value IS NOT NULL
      AND timestamp_utc >= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
            - (CAST(? AS DOUBLE) * INTERVAL '1' HOUR)
),
agg AS (
    SELECT
        equipment_id,
        series_id,
        sensor_role,
        MAX(value) - MIN(value) AS spread,
        COUNT(*) AS n,
        MIN(timestamp_utc) AS first_ts,
        MAX(timestamp_utc) AS last_ts
    FROM windowed
    GROUP BY equipment_id, series_id, sensor_role
)
SELECT
    equipment_id,
    series_id,
    sensor_role,
    CASE WHEN n >= 2 AND spread <= CAST(? AS DOUBLE) THEN
        EPOCH(last_ts - first_ts) / 3600.0
    ELSE 0.0 END AS fault_hours,
    GREATEST(EPOCH(last_ts - first_ts) / 3600.0, 1.0 / 60.0) AS covered_hours
FROM agg
WHERE n >= 2;
