-- SV-RANGE: latest SI temperature (degC) outside [range_lo, range_hi].
-- Other quantity kinds are not assessed by this simplified edge rule.
-- Parameters: range_lo (DOUBLE degC), range_hi (DOUBLE degC)
-- Output: equipment_id, series_id, sensor_role, fault_hours, covered_hours
WITH latest AS (
    SELECT
        equipment_id,
        series_id,
        sensor_role,
        unit,
        arg_max(value, timestamp_utc) AS value,
        MAX(timestamp_utc) AS last_ts,
        MIN(timestamp_utc) AS first_ts
    FROM sensor_history
    WHERE value IS NOT NULL
      AND lower(COALESCE(unit, '')) IN ('degc', 'c', 'cel', 'degree_celsius', 'deg_c')
    GROUP BY equipment_id, series_id, sensor_role, unit
)
SELECT
    equipment_id,
    series_id,
    sensor_role,
    CASE
        WHEN value < CAST(? AS DOUBLE) OR value > CAST(? AS DOUBLE) THEN 1.0
        ELSE 0.0
    END AS fault_hours,
    GREATEST(
        EPOCH((CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - first_ts) / 3600.0,
        1.0 / 60.0
    ) AS covered_hours
FROM latest;
