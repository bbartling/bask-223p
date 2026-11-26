"""223P snake_case names from the Open-FDD UI contract (issue #1770).

Units are the OpenAPI ``SiUnit`` spellings. A parity test reads
``contract/open-fdd-ui-api.yaml`` and fails if this table drifts from that file.
"""
from __future__ import annotations

# Names are the InputName examples in the UI contract. Units are the SI symbol
# the wire uses for that quantity kind.
ROLE_UNITS: dict[str, str] = {
    "discharge_air_temperature": "°C",
    "mixed_air_temperature": "°C",
    "outside_air_temperature": "°C",
    "return_air_temperature": "°C",
    "discharge_air_temperature_setpoint": "°C",
    "zone_air_temperature": "°C",
    "supply_fan_run_status": "",
    "supply_fan_command": "",
    "outside_air_damper_position": "%",
    "cooling_valve_position": "%",
    "heating_valve_position": "%",
    "chilled_water_supply_temperature": "°C",
    "hot_water_supply_temperature": "°C",
    "duct_static_pressure": "Pa",
}

INPUT_NAME = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"
