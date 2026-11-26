-- SV-STALE: a series is stale when it has no valid sample for longer than
-- STALE_AFTER_HOURS. No value test (a stuck but still-polled value is SV-FLATLINE).
-- Parameters: stale_after_hours (DOUBLE)
-- Output: equipment_id, series_id, sensor_role, fault_hours, covered_hours
WITH bounds AS (
    SELECT
        equipment_id,
        series_id,
        sensor_role,
        MIN(timestamp_utc) AS first_ts,
        MAX(timestamp_utc) AS last_ts,
        COUNT(*) AS n
    FROM sensor_history
    WHERE value IS NOT NULL
    GROUP BY equipment_id, series_id, sensor_role
),
scored AS (
    SELECT
        equipment_id,
        series_id,
        sensor_role,
        first_ts,
        last_ts,
        GREATEST(
            EPOCH(CURRENT_TIMESTAMP AT TIME ZONE 'UTC' - first_ts) / 3600.0,
            CAST(? AS DOUBLE)
        ) AS covered_hours,
        GREATEST(
            0.0,
            EPOCH((CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - last_ts) / 3600.0
                - CAST(? AS DOUBLE)
        ) AS fault_hours
    FROM bounds
)
SELECT
    equipment_id,
    series_id,
    sensor_role,
    fault_hours,
    covered_hours
FROM scored;
