"""SI unit normalization and conversion for edge telemetry & Parquet export."""
from __future__ import annotations

from typing import Tuple

try:
    from bask_223p.bacnet.units import resolve_qudt_unit as _resolve_qudt_unit
except Exception:  # pragma: no cover - fallback if bask_223p is not importable
    _resolve_qudt_unit = None

_EXTRA_QUDT_ALIASES: dict[str, str] = {
    "c": "DEG_C",
    "cel": "DEG_C",
    "degc": "DEG_C",
    "deg_c": "DEG_C",
    "°c": "DEG_C",
    "f": "DEG_F",
    "degf": "DEG_F",
    "deg_f": "DEG_F",
    "°f": "DEG_F",
    "k": "DEG_K",
    "degk": "DEG_K",
    "deg_k": "DEG_K",
    "pa": "PA",
    "kpa": "KiloPA",
    "psi": "PSI",
    "inh2o": "IN_H2O",
    "in_h2o": "IN_H2O",
    "inwc": "IN_H2O",
    "cfm": "FT3-PER-MIN",
    "ft3/min": "FT3-PER-MIN",
    "ft3-per-min": "FT3-PER-MIN",
    "m3/s": "M3-PER-SEC",
    "m3-per-sec": "M3-PER-SEC",
    "gpm": "GAL_US-PER-MIN",
    "w": "W",
    "kw": "KiloW",
    "kwh": "KiloW-HR",
    "j": "J",
    "hz": "HZ",
    "%": "PERCENT",
    "percent": "PERCENT",
    "%rh": "PERCENT_RH",
    "percent_rh": "PERCENT_RH",
    "ppm": "PPM",
    "b": "BYTE",
    "byte": "BYTE",
    "bytes": "BYTE",
}


def _qudt_token(raw_unit: str | None) -> str | None:
    raw = (raw_unit or "").strip()
    if not raw:
        return None
    if _resolve_qudt_unit is not None:
        hit = _resolve_qudt_unit(raw)
        if hit:
            return hit
    low = raw.casefold().replace(" ", "")
    if low in _EXTRA_QUDT_ALIASES:
        return _EXTRA_QUDT_ALIASES[low]
    return None


def to_si(value: float | None, raw_unit: str | None) -> Tuple[float | None, str]:
    """Convert a numeric value and engineering unit into SI units.

    Returns ``(converted_value, si_unit_symbol)``. Unknown units pass through
    unchanged so callers never lose information.
    """
    token = _qudt_token(raw_unit)
    if token == "DEG_F":
        return (((value - 32.0) * 5.0 / 9.0) if value is not None else None), "degC"
    if token == "DEG_C":
        return value, "degC"
    if token == "DEG_K":
        return value, "K"
    if token == "IN_H2O":
        return ((value * 249.08891) if value is not None else None), "Pa"
    if token == "PSI":
        return ((value * 6894.757293) if value is not None else None), "Pa"
    if token == "KiloPA":
        return ((value * 1000.0) if value is not None else None), "Pa"
    if token == "PA":
        return value, "Pa"
    if token == "FT3-PER-MIN":
        return ((value * 0.00047194745) if value is not None else None), "m3/s"
    if token == "GAL_US-PER-MIN":
        return ((value * 6.30901964e-5) if value is not None else None), "m3/s"
    if token == "M3-PER-SEC":
        return value, "m3/s"
    if token == "KiloW":
        return ((value * 1000.0) if value is not None else None), "W"
    if token == "W":
        return value, "W"
    if token == "KiloW-HR":
        return ((value * 3_600_000.0) if value is not None else None), "J"
    if token == "HZ":
        return value, "Hz"
    if token == "PERCENT":
        return value, "%"
    if token == "PERCENT_RH":
        return value, "%RH"
    if token == "PPM":
        return value, "ppm"
    if token == "BYTE":
        return value, "B"
    return value, (raw_unit or "").strip()


def normalize_si_unit(raw_unit: str | None) -> str:
    """Return the SI unit string for a raw engineering unit."""
    return to_si(None, raw_unit)[1]
