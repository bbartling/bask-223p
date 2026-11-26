"""223P-derived snake_case rule-input terms (open-fdd#1804).

Open-FDD acronyms (`sat`, `mat`, `oa_t`, ...) are never stored or used as
SQL aliases. Haystack kebab-case tags are mapped to the same snake_case terms.
"""
from __future__ import annotations

import re
from typing import Any

# Acronyms retired by open-fdd#1804 and their 223P-derived replacements.
ACRONYM_TO_223P: dict[str, str] = {
    "sat": "discharge_air_temperature",
    "sat_sp": "discharge_air_temperature_setpoint",
    "mat": "mixed_air_temperature",
    "rat": "return_air_temperature",
    "oa_t": "outside_air_temperature",
    "oa_h": "outside_air_humidity",
    "zone_t": "zone_air_temperature",
    "fan_status": "supply_fan_run_status",
    "fan_cmd": "supply_fan_command",
    "oa_damper_pct": "outside_air_damper_position",
    "clg_valve_pct": "cooling_valve_position",
    "htg_valve_pct": "heating_valve_position",
    "chw_supply_t": "chilled_water_supply_temperature",
    "hw_supply_t": "hot_water_supply_temperature",
    "duct_static": "duct_static_pressure",
    "run_proof": "run_status",
}

# Haystack-style kebab-case (Open-FDD role_map.json values) -> 223P snake_case.
HAYSTACK_TO_223P: dict[str, str] = {
    "discharge-air-temp": "discharge_air_temperature",
    "discharge-air-temp-sp": "discharge_air_temperature_setpoint",
    "mixed-air-temp": "mixed_air_temperature",
    "return-air-temp": "return_air_temperature",
    "outside-air-temp": "outside_air_temperature",
    "outside-air-humidity": "outside_air_humidity",
    "outside-air-damper": "outside_air_damper_position",
    "cooling-valve": "cooling_valve_position",
    "heating-valve": "heating_valve_position",
    "fan-status": "supply_fan_run_status",
    "fan-cmd": "supply_fan_command",
    "duct-static-pressure": "duct_static_pressure",
    "duct-static-pressure-sp": "duct_static_pressure_setpoint",
    "zone-air-temp": "zone_air_temperature",
    "zone-air-temp-sp": "zone_air_temperature_setpoint",
    "chilled-water-supply-temp": "chilled_water_supply_temperature",
    "hot-water-supply-temp": "hot_water_supply_temperature",
    "discharge-air-flow": "discharge_air_volumetric_flow",
    "zone-air-flow": "zone_air_volumetric_flow",
}

_TOKEN_STRIP = re.compile(r"[^a-z0-9]+")
_SPACEY = re.compile(r"[\s_]+")


def _kebab(text: str) -> str:
    s = _SPACEY.sub("-", text.strip().lower())
    s = s.replace("°", "")
    s = re.sub(r"-sensor$", "", s)
    s = re.sub(r"-point$", "", s)
    return s.strip("-")


def _expand_haystack(kebab: str) -> str:
    s = kebab
    s = re.sub(r"(?<![a-z])temp(?![a-z])", "temperature", s)
    s = re.sub(r"-temp$", "-temperature", s)
    s = re.sub(r"-temp-", "-temperature-", s)
    s = re.sub(r"-sp$", "-setpoint", s)
    s = re.sub(r"-cmd$", "-command", s)
    s = s.replace("-humidity", "-humidity")
    return s.replace("-", "_")


def to_input_term(raw: str | None) -> str:
    """Map a role, tag, or label to a 223P-derived snake_case term, or ''."""
    if not raw:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    key = text.lower().replace(" ", "_")
    if key in ACRONYM_TO_223P:
        return ACRONYM_TO_223P[key]
    kebab = _kebab(text)
    if kebab in HAYSTACK_TO_223P:
        return HAYSTACK_TO_223P[kebab]
    if kebab in ACRONYM_TO_223P:
        return ACRONYM_TO_223P[kebab]
    # Already a snake_case term with no retired acronym.
    if re.fullmatch(r"[a-z][a-z0-9_]*", text) and text not in ACRONYM_TO_223P:
        if text.endswith("_t") and text in ACRONYM_TO_223P:
            return ACRONYM_TO_223P[text]
        return text
    return _expand_haystack(kebab)


def input_term_from_target(target: Any) -> str:
    """Best-effort 223P input term from a PointTarget (or similar)."""
    extra = getattr(target, "extra", None) or {}
    if not isinstance(extra, dict):
        extra = {}
    for key in ("input_term", "s223_input_term"):
        term = to_input_term(extra.get(key))
        if term:
            return term
    term = to_input_term(extra.get("role"))
    if term:
        return term
    tags = extra.get("haystack_tags") or extra.get("tags") or ""
    if isinstance(tags, (list, tuple, set)):
        for tag in tags:
            term = to_input_term(str(tag))
            if term:
                return term
    else:
        # Prefer the full phrase (e.g. "zone air temp sensor") over split tokens.
        term = to_input_term(str(tags))
        if term:
            return term
    label = getattr(target, "label", None)
    term = to_input_term(label)
    if term:
        return term
    return ""


# Retired acronyms that must not appear as SQL aliases or role values.
RETIRED_ACRONYMS: frozenset[str] = frozenset(ACRONYM_TO_223P)
