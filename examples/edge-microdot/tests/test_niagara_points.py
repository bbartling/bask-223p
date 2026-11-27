"""The Niagara poll set is the configured points, not a station browse."""
from __future__ import annotations

from pathlib import Path

from edge_microdot.models import PointTarget
from edge_microdot.sql_guard import validate_read_only_sql
from edge_microdot.sources.baskstream import BaskStreamSource


def test_empty_targets_do_not_browse_the_station():
    src = BaskStreamSource(station="https://station.example", targets=[])

    def boom() -> list[PointTarget]:
        raise AssertionError("station browse")

    src.discover_targets = boom  # type: ignore[method-assign]
    assert src._ensure_targets(None) == []  # type: ignore[arg-type]
    assert src._ensure_targets(None) == []  # type: ignore[arg-type]


def test_ord_fill_does_not_replace_the_configured_points():
    configured = PointTarget(site="bench", device="10001", object_id="analog-input,1")
    src = BaskStreamSource(station="https://station.example", targets=[configured])
    extra = PointTarget(
        site="bench",
        device="10001",
        object_id="analog-input,2",
        ord_path="slot:/Drivers/BacnetNetwork/extra",
    )
    match = PointTarget(
        site="bench",
        device="10001",
        object_id="analog-input,1",
        ord_path="slot:/Drivers/BacnetNetwork/zone",
    )

    def only_these() -> list[PointTarget]:
        return [extra, match]

    src.discover_targets = only_these  # type: ignore[method-assign]
    found = src._ensure_targets(None)  # type: ignore[arg-type]
    assert [t.object_id for t in found] == ["analog-input,1"]
    assert found[0].ord_path == "slot:/Drivers/BacnetNetwork/zone"
    assert src.targets == found


def test_rule_files_are_read_only_sql():
    rules = Path(__file__).resolve().parents[1] / "sql" / "fdd" / "rules"
    names = sorted(path.name for path in rules.glob("*.sql"))
    assert names
    for path in rules.glob("*.sql"):
        validate_read_only_sql(path.read_text(encoding="utf-8"))
