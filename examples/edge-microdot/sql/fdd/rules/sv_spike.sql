-- SV-SPIKE: a consecutive SI sample step larger than SPIKE_THRESHOLD.
-- Parameters: spike_threshold (DOUBLE, SI)
-- Output: equipment_id, series_id, sensor_role, fault_hours, covered_hours
WITH ordered AS (
    SELECT
        equipment_id,
        series_id,
        sensor_role,
        value,
        timestamp_utc,
        LAG(value) OVER (
            PARTITION BY equipment_id, series_id ORDER BY timestamp_utc
        ) AS prev_value,
        MIN(timestamp_utc) OVER (PARTITION BY equipment_id, series_id) AS first_ts,
        MAX(timestamp_utc) OVER (PARTITION BY equipment_id, series_id) AS last_ts
    FROM sensor_history
    WHERE value IS NOT NULL
),
hits AS (
    SELECT
        equipment_id,
        series_id,
        sensor_role,
        first_ts,
        last_ts,
        COUNT(*) FILTER (
            WHERE prev_value IS NOT NULL
              AND abs(value - prev_value) > CAST(? AS DOUBLE)
        ) AS n_spikes
    FROM ordered
    GROUP BY equipment_id, series_id, sensor_role, first_ts, last_ts
)
SELECT
    equipment_id,
    series_id,
    sensor_role,
    CASE WHEN n_spikes > 0 THEN 1.0 ELSE 0.0 END AS fault_hours,
    GREATEST(EPOCH(last_ts - first_ts) / 3600.0, 1.0 / 60.0) AS covered_hours
FROM hits;
