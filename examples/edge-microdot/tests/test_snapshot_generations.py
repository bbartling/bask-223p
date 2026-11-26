"""Snapshot generations: prune oldest, keep the newest set, reload that set only."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import duckdb

from edge_microdot.history_store import HistoryStore
from edge_microdot.models import PointSample, PointTarget


def _sample(value: float, *, obj: str = "analog-input,1") -> PointSample:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return PointSample(
        timestamp_utc=ts,
        site="demo",
        source="fake",
        device="10",
        object=obj,
        property="present-value",
        value=value,
        units="degC",
    )


def _rule(subject: str) -> dict:
    return {
        "site_id": "demo",
        "equipment_id": "AHU-1",
        "rule_id": "SV-STALE",
        "subject_key": subject,
        "status": "FAULT",
        "reason": "test",
        "fault_hours": 1.0,
        "covered_hours": 1.0,
        "role": "zone_air_temperature",
        "window_end": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }


def _write_legacy(directory, name: str, nbytes: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t AS SELECT 1 AS ref, CURRENT_TIMESTAMP AS timestamp_utc")
    path = directory / name
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    con.close()
    # Pad so the byte cap sees real size even for a tiny parquet.
    blob = path.read_bytes()
    if len(blob) < nbytes:
        path.write_bytes(blob + b"\x00" * (nbytes - len(blob)))


def test_legacy_files_do_not_evict_the_new_snapshot(tmp_path):
    snap = tmp_path / "snapshots"
    for i in range(3):
        _write_legacy(snap, f"telemetry_2020010{i}T000000Z.parquet", 4000)
        _write_legacy(snap, f"fdd_results_2020010{i}T000000Z.parquet", 1000)
    store = HistoryStore(snap, max_bytes=5000, max_snapshots=2)
    store.stage([_sample(21.0)])
    store.flush()
    written = store.write_snapshot(force=True)
    assert written is not None and written.exists()
    assert not list(snap.glob("telemetry_*.parquet"))
    assert not list(snap.glob("fdd_results_*.parquet"))
    gens = [p for p in snap.iterdir() if p.is_dir() and p.name.startswith("gen_")]
    assert len(gens) == 1
    manifest = json.loads((gens[0] / "manifest.json").read_text(encoding="utf-8"))
    assert "sample_store.parquet" in manifest["files"]
    assert "point.parquet" in manifest["files"]
    assert (gens[0] / "sample_store.parquet").is_file()
    assert (gens[0] / "point.parquet").is_file()
    store.close()

    reloaded = HistoryStore(snap, max_bytes=5000, max_snapshots=2)
    assert reloaded.row_count() == 1
    assert reloaded.query("SELECT equipment_id FROM fdd.point")[0][0]
    reloaded.close()


def test_byte_cap_keeps_newest_generation_intact(tmp_path):
    snap = tmp_path / "snapshots"
    old = snap / "gen_000001"
    old.mkdir(parents=True)
    (old / "sample_store.parquet").write_bytes(b"old-sample" + b"x" * 8000)
    (old / "point.parquet").write_bytes(b"old-point")
    (old / "manifest.json").write_text(
        json.dumps({"seq": 1, "files": ["sample_store.parquet", "point.parquet"], "written_utc": "2020-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    store = HistoryStore(snap, max_bytes=3000, max_snapshots=5)
    store.stage([_sample(19.5)])
    store.flush()
    store.upsert_points(
        [PointTarget(site="demo", device="10", object_id="analog-input,1", units="degC", extra={"equipment": "AHU-NEW"})]
    )
    store.write_snapshot(force=True)
    names = sorted(p.name for p in snap.iterdir() if p.is_dir() and p.name.startswith("gen_"))
    assert names == ["gen_000002"]
    kept = snap / "gen_000002"
    assert (kept / "manifest.json").is_file()
    assert (kept / "sample_store.parquet").is_file()
    assert (kept / "point.parquet").is_file()
    store.close()

    reloaded = HistoryStore(snap, max_bytes=3000, max_snapshots=5)
    assert reloaded.query("SELECT equipment_id FROM fdd.point") == [("AHU-NEW",)]
    assert reloaded.row_count() == 1
    reloaded.close()


def test_count_cap_drops_oldest_modern_generation(tmp_path):
    snap = tmp_path / "snapshots"
    store = HistoryStore(snap, max_bytes=50 * 1024 * 1024, max_snapshots=2)
    for i, equip in enumerate(("AHU-1", "AHU-2", "AHU-3")):
        store.upsert_points(
            [
                PointTarget(
                    site="demo",
                    device="10",
                    object_id=f"analog-input,{i}",
                    units="degC",
                    extra={"equipment": equip},
                )
            ]
        )
        store.stage([_sample(float(i), obj=f"analog-input,{i}")])
        store.flush()
        assert store.write_snapshot(force=True) is not None
    gens = sorted(p.name for p in snap.iterdir() if p.is_dir() and p.name.startswith("gen_"))
    assert gens == ["gen_000002", "gen_000003"]
    store.close()
    reloaded = HistoryStore(snap, max_bytes=50 * 1024 * 1024, max_snapshots=2)
    assert reloaded.row_count() == 3
    reloaded.close()


def test_tmp_orphans_are_removed_and_not_counted(tmp_path):
    snap = tmp_path / "snapshots"
    snap.mkdir()
    (snap / ".sample_store_orphan.parquet.tmp").write_bytes(b"x" * 5000)
    staging = snap / ".gen_000009.tmp"
    staging.mkdir()
    (staging / "sample_store.parquet").write_bytes(b"y" * 5000)
    store = HistoryStore(snap, max_bytes=1000, max_snapshots=2)
    assert store.disk_bytes() == 0
    assert not (snap / ".sample_store_orphan.parquet.tmp").exists()
    assert not staging.exists()
    store.close()


def test_reload_uses_one_generation(tmp_path):
    snap = tmp_path / "snapshots"
    store = HistoryStore(snap, max_bytes=50 * 1024 * 1024, max_snapshots=5)
    store.stage([_sample(1.0)])
    store.flush()
    store.upsert_points(
        [PointTarget(site="demo", device="10", object_id="analog-input,1", units="degC", extra={"equipment": "AHU-OLD"})]
    )
    store.write_snapshot(force=True)
    store.stage([_sample(2.0, obj="analog-input,2")])
    store.flush()
    store.upsert_points(
        [PointTarget(site="demo", device="10", object_id="analog-input,1", units="degC", extra={"equipment": "AHU-NEW"})]
    )
    store.write_snapshot(force=True)
    # A newer-looking loose point file must not replace the generation's point table.
    loose = snap / "point_99999999T000000000000Z.parquet"
    gen_point = snap / "gen_000001" / "point.parquet"
    loose.write_bytes(gen_point.read_bytes())
    store.close()

    reloaded = HistoryStore(snap, max_bytes=50 * 1024 * 1024, max_snapshots=5)
    assert reloaded.query("SELECT equipment_id FROM fdd.point WHERE point_key LIKE '%analog-input,1'") == [("AHU-NEW",)]
    assert reloaded.row_count() == 2
    reloaded.close()


def test_rule_result_ids_continue_after_reload(tmp_path):
    snap = tmp_path / "snapshots"
    store = HistoryStore(snap)
    store.stage([_sample(1.0)])
    store.flush()
    assert store.record_rule_results([_rule("a")]) == 1
    assert store.record_rule_results([_rule("b")]) == 1
    before = [row[0] for row in store.query("SELECT result_id FROM fdd.rule_result ORDER BY result_id")]
    assert before == [1, 2]
    store.write_snapshot(force=True)
    store.close()

    reloaded = HistoryStore(snap)
    assert [row[0] for row in reloaded.query("SELECT result_id FROM fdd.rule_result ORDER BY result_id")] == [1, 2]
    reloaded.record_rule_results([_rule("c")])
    ids = [row[0] for row in reloaded.query("SELECT result_id FROM fdd.rule_result ORDER BY result_id")]
    assert ids == [1, 2, 3]
    reloaded.close()


def test_snapshot_skipped_when_clock_is_behind_last_generation(tmp_path):
    snap = tmp_path / "snapshots"
    store = HistoryStore(snap)
    store.stage([_sample(1.0)])
    store.flush()
    store.write_snapshot(force=True)
    store.close()
    manifest = next(snap.glob("gen_*/manifest.json"))
    body = json.loads(manifest.read_text(encoding="utf-8"))
    body["written_utc"] = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest.write_text(json.dumps(body), encoding="utf-8")

    again = HistoryStore(snap)
    again.stage([_sample(2.0, obj="analog-input,2")])
    again.flush()
    assert again.write_snapshot(force=True) is None
    assert len(list(snap.glob("gen_*"))) == 1
    again.close()
