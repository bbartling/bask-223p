"""Shared BACnet / Niagara unit and QUDT QuantityKind normalization with caching."""
from __future__ import annotations

import re
from functools import lru_cache

from rdflib import Namespace, URIRef

QUDT = Namespace("http://qudt.org/schema/qudt/")
UNIT = Namespace("http://qudt.org/vocab/unit/")
QK = Namespace("http://qudt.org/vocab/quantitykind/")

# Conservative Niagara unit display / facet token -> QUDT unit local name.
QUDT_UNIT_MAP: dict[str, str] = {
    "°f": "DEG_F",
    "degf": "DEG_F",
    "fahrenheit": "DEG_F",
    "degreesfahrenheit": "DEG_F",
    "°c": "DEG_C",
    "degc": "DEG_C",
    "celsius": "DEG_C",
    "degreescelsius": "DEG_C",
    "°k": "DEG_K",
    "degk": "DEG_K",
    "kelvin": "DEG_K",
    "degreeskelvin": "DEG_K",
    "%": "PERCENT",
    "percent": "PERCENT",
    "%rh": "PERCENT_RH",
    "percentrelativehumidity": "PERCENT_RH",
    "inh2o": "IN_H2O",
    "inh₂o": "IN_H2O",
    "inchesofwater": "IN_H2O",
    "cfm": "FT3-PER-MIN",
    "cubicfeetperminute": "FT3-PER-MIN",
    "m3/s": "M3-PER-SEC",
    "cubicmeterspersecond": "M3-PER-SEC",
    "psi": "PSI",
    "poundsforcepersquareinch": "PSI",
    "pa": "PA",
    "pascals": "PA",
    "kpa": "KiloPA",
    "kilopascals": "KiloPA",
    "kw": "KiloW",
    "kilowatts": "KiloW",
    "kwh": "KiloW-HR",
    "kilowatthours": "KiloW-HR",
    "w": "W",
    "watts": "W",
    "hz": "HZ",
    "hertz": "HZ",
    "gpm": "GAL_US-PER-MIN",
    "gallonsperminute": "GAL_US-PER-MIN",
    "usgallonsperminute": "GAL_US-PER-MIN",
    "ppm": "PPM",
    "partspermillion": "PPM",
}

# BACnet EngineeringUnits enumeration names -> QUDT local names.
BACNET_UNIT_QUDT: dict[str, str] = {
    "degrees-fahrenheit": "DEG_F",
    "degrees-celsius": "DEG_C",
    "degrees-kelvin": "DEG_K",
    "percent": "PERCENT",
    "percent-relative-humidity": "PERCENT_RH",
    "inches-of-water": "IN_H2O",
    "cubic-feet-per-minute": "FT3-PER-MIN",
    "cubic-meters-per-second": "M3-PER-SEC",
    "pounds-force-per-square-inch": "PSI",
    "pascals": "PA",
    "kilopascals": "KiloPA",
    "kilowatts": "KiloW",
    "kilowatt-hours": "KiloW-HR",
    "watts": "W",
    "hertz": "HZ",
    "us-gallons-per-minute": "GAL_US-PER-MIN",
    "parts-per-million": "PPM",
}

# QUDT quantity kind local name, taken from the QUDT unit (not from point name).
QUANTITY_KIND_FROM_UNIT: dict[str, str] = {
    "DEG_F": "Temperature",
    "DEG_C": "Temperature",
    "DEG_K": "Temperature",
    "IN_H2O": "Pressure",
    "PA": "Pressure",
    "KiloPA": "Pressure",
    "PSI": "Pressure",
    "PERCENT": "Dimensionless",
    "PERCENT_RH": "RelativeHumidity",
    "FT3-PER-MIN": "VolumeFlowRate",
    "M3-PER-SEC": "VolumeFlowRate",
    "GAL_US-PER-MIN": "VolumeFlowRate",
    "W": "Power",
    "KiloW": "Power",
    "KiloW-HR": "Energy",
    "HZ": "Frequency",
    "PPM": "DimensionlessRatio",
}

_NULL_UNIT_TOKENS = frozenset({"", "null", "none", "no-units", "nounits"})
_COMPACT_RE = re.compile(r"[\s_\-]+")


@lru_cache(maxsize=512)
def resolve_qudt_unit(unit_text: str | None) -> str | None:
    """Resolve a Niagara facet unit, BACnet EngineeringUnits token/IRI, or QUDT local name."""
    raw = (unit_text or "").strip()
    if not raw:
        return None
    if "#" in raw:
        raw = raw.rsplit("#", 1)[-1].strip()
    if "EngineeringUnits." in raw:
        raw = raw.split("EngineeringUnits.", 1)[-1].strip()
    if raw.startswith("u:"):
        raw = raw[2:].strip()
    if raw.casefold() in _NULL_UNIT_TOKENS:
        return None
    # Allow already-normalized QUDT local names (e.g. DEG_F, FT3-PER-MIN).
    if raw in QUANTITY_KIND_FROM_UNIT:
        return raw

    candidates = [raw]
    if ";" in raw:
        candidates.extend(p.strip() for p in raw.split(";")[:2] if p.strip())

    for piece in candidates:
        low = piece.casefold()
        if low in _NULL_UNIT_TOKENS:
            continue
        hit = BACNET_UNIT_QUDT.get(low)
        if hit:
            return hit
        compact = _COMPACT_RE.sub("", low)
        hit = QUDT_UNIT_MAP.get(compact) or QUDT_UNIT_MAP.get(low)
        if hit:
            return hit
    return None


def map_qudt_unit(unit_text: str | None) -> str | None:
    """Cached Niagara/BACnet unit resolver (backwards-compatible alias)."""
    return resolve_qudt_unit(unit_text)


@lru_cache(maxsize=256)
def map_quantity_kind_name(unit_local: str | None) -> str | None:
    """Return the QUDT QuantityKind local name for a known QUDT unit local name."""
    if not unit_local:
        return None
    return QUANTITY_KIND_FROM_UNIT.get(unit_local)


def map_quantity_kind(unit_local: str | None) -> URIRef | None:
    """QUDT QuantityKind IRI for a known unit. Returns None rather than guessing."""
    name = map_quantity_kind_name(unit_local)
    return QK[name] if name else None


@lru_cache(maxsize=512)
def resolve_unit_and_quantity_kind(unit_text: str | None) -> tuple[str | None, str | None]:
    """Resolve raw unit text into cached ``(qudt_unit_local, quantity_kind_local)``."""
    qudt_unit = resolve_qudt_unit(unit_text)
    if not qudt_unit:
        return None, None
    return qudt_unit, map_quantity_kind_name(qudt_unit)


__all__ = [
    "BACNET_UNIT_QUDT",
    "QK",
    "QUANTITY_KIND_FROM_UNIT",
    "QUDT",
    "QUDT_UNIT_MAP",
    "UNIT",
    "map_quantity_kind",
    "map_quantity_kind_name",
    "map_qudt_unit",
    "resolve_qudt_unit",
    "resolve_unit_and_quantity_kind",
]
