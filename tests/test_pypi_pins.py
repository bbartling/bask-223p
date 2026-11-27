"""The pin checker retries a dropped PyPI lookup and reports a stale pin."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from urllib.error import URLError

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _checker():
    path = ROOT / "scripts" / "check_pypi_pins.py"
    spec = importlib.util.spec_from_file_location("check_pypi_pins", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_pins_reads_direct_and_optional_requirements(tmp_path: Path):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        """
[project]
dependencies = ["bacpypes3==0.1.0", "rdflib>=7"]
[project.optional-dependencies]
dev = ["httpx==0.29.0"]
modbus = ["pymodbus==3.16.2"]
""",
        encoding="utf-8",
    )
    found = _checker().pins(path)
    assert found == [("bacpypes3", "0.1.0"), ("httpx", "0.29.0"), ("pymodbus", "3.16.2")]


def test_latest_retries_a_dropped_handshake():
    calls = {"n": 0}

    def opener(url, timeout=30):
        assert "bacpypes3" in url
        assert timeout == 30
        calls["n"] += 1
        if calls["n"] == 1:
            raise URLError("handshake timeout")
        body = io.BytesIO(json.dumps({"info": {"version": "0.1.0"}}).encode())
        body.__enter__ = lambda: body
        body.__exit__ = lambda *args: False
        return body

    assert _checker().latest("bacpypes3", attempts=3, sleep=lambda _seconds: None, urlopen=opener) == "0.1.0"
    assert calls["n"] == 2


def test_latest_fails_after_the_last_attempt():
    def opener(url, timeout=30):
        raise URLError("handshake timeout")

    with pytest.raises(RuntimeError, match="bacpypes3"):
        _checker().latest("bacpypes3", attempts=2, sleep=lambda _seconds: None, urlopen=opener)
