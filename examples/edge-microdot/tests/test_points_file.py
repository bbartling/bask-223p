"""EDGE_POINTS_FILE is the points file when --points is omitted."""
from __future__ import annotations

import logging

from edge_microdot.cli import build_runtime, points_file_from_env, resolve_points_path


CSV = 'device,object,property,units,label\n10001,"analog-input,1",present-value,degC,Zone temperature\n'
OTHER = 'device,object,property,units,label\n20002,"analog-value,2",present-value,percent,Damper\n'


class _Source:
    def __init__(self, targets, **kwargs):
        self.targets = list(targets)
        self.kwargs = kwargs

    async def close(self):
        return None


def test_default_points_path_and_cli_override(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGE_POINTS_FILE", raising=False)
    assert points_file_from_env() == __import__("pathlib").Path(
        "/app/examples/edge-microdot/var/points.csv"
    )
    missing = tmp_path / "missing.csv"
    monkeypatch.setenv("EDGE_POINTS_FILE", str(missing))
    assert resolve_points_path(None) is None

    fallback = tmp_path / "points.csv"
    fallback.write_text(CSV, encoding="utf-8")
    monkeypatch.setenv("EDGE_POINTS_FILE", str(fallback))
    assert resolve_points_path(None) == fallback

    explicit = tmp_path / "other.csv"
    explicit.write_text(OTHER, encoding="utf-8")
    assert resolve_points_path(explicit) == explicit


def test_runtime_loads_env_file_unless_flag_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr("edge_microdot.cli.BacnetRpmSource", _Source)
    fallback = tmp_path / "points.csv"
    fallback.write_text(CSV, encoding="utf-8")
    explicit = tmp_path / "other.csv"
    explicit.write_text(OTHER, encoding="utf-8")
    monkeypatch.setenv("EDGE_POINTS_FILE", str(fallback))
    monkeypatch.setenv("EDGE_BACNET_ADDRESS", "192.0.2.10/24")

    _app, source, _hot, history, *_rest = build_runtime(
        ["--driver", "bacnet", "--db", str(tmp_path / "a.duckdb"), "--devices", "54545"]
    )
    try:
        assert [t.device for t in source.targets] == ["10001"]
        assert source.targets[0].object_id == "analog-input,1"
        assert source.targets[0].property_id == "present-value"
        assert source.targets[0].units == "degC"
        assert source.targets[0].label == "Zone temperature"
        assert source.kwargs["discover_instances"] == [54545]
    finally:
        history.close()

    _app, source, _hot, history, *_rest = build_runtime(
        [
            "--driver",
            "bacnet",
            "--points",
            str(explicit),
            "--db",
            str(tmp_path / "b.duckdb"),
            "--devices",
            "54545",
        ]
    )
    try:
        assert [t.device for t in source.targets] == ["20002"]
        assert source.targets[0].label == "Damper"
    finally:
        history.close()


def test_missing_points_file_is_logged(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("edge_microdot.cli.BacnetRpmSource", _Source)
    missing = tmp_path / "missing.csv"
    monkeypatch.setenv("EDGE_POINTS_FILE", str(missing))
    monkeypatch.setenv("EDGE_BACNET_ADDRESS", "192.0.2.10/24")
    caplog.set_level(logging.WARNING)
    _app, source, _hot, history, *_rest = build_runtime(
        ["--driver", "bacnet", "--db", str(tmp_path / "c.duckdb"), "--devices", "54545"]
    )
    try:
        assert source.targets == []
        text = caplog.text
        assert "no points are configured" in text
        assert str(missing) in text
        assert "device,object,property,units,label" in text
    finally:
        history.close()
