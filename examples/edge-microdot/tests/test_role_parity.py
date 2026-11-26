"""Fail when 223P role names or SI units drift from the Open-FDD UI contract."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from edge_microdot.role_contract import ROLE_UNITS
from edge_microdot.ui_api import applicability_lines

SPEC = Path(__file__).resolve().parents[1] / "contract" / "open-fdd-ui-api.yaml"
SI = {"°C", "Δ°C", "Δ°C/h", "Pa", "L/s", "kW", "kWh", "%", "ppm", "h", ""}


def test_role_names_match_openapi_input_examples():
    text = SPEC.read_text(encoding="utf-8")
    spec = yaml.safe_load(text)
    enum = set(spec["components"]["schemas"]["SiUnit"]["enum"])
    assert enum == SI
    described = spec["components"]["schemas"]["InputName"]["description"]
    names = re.findall(r"^\s*\|\s*`([a-z][a-z0-9_]*)`\s*\|", described, flags=re.M)
    assert names
    assert list(ROLE_UNITS) == names
    assert set(ROLE_UNITS.values()) <= enum


def test_applicability_skips_a_rule_whose_role_is_missing():
    rules = [
        {
            "rule_id": "FC1",
            "required": ["outside_air_temperature", "mixed_air_temperature"],
        }
    ]
    lines = applicability_lines({"outside_air_temperature"}, rules)
    assert lines == ["FC1 skipped: missing mixed_air_temperature"]
