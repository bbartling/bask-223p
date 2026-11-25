from __future__ import annotations

from pathlib import Path

from live_ingest.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_ROOT = Path(__file__).resolve().parents[1]


def test_load_223p_ttl():
    cfg = load_config(REPO_ROOT / "examples" / "illustrative-ahu-refs.ttl", site="lab-1")
    assert cfg.graph is not None
    assert len(cfg.targets) >= 5
    assert all(t.ref.startswith("bacnet://") for t in cfg.targets)


def test_load_bacpypes3_scan_ttl():
    cfg = load_config(REPO_ROOT / "examples" / "bench-scan.ttl", site="lab-1")
    assert cfg.graph is not None
    assert len(cfg.targets) >= 4
    assert all(t.ref.startswith("bacnet://") for t in cfg.targets)


def test_load_csv_and_yaml():
    csv_cfg = load_config(LIVE_ROOT / "configs" / "sample-bacnet-points.csv")
    assert len(csv_cfg.targets) == 4
    assert csv_cfg.targets[0].ref == "bacnet://10001/analog-input,1"

    yaml_cfg = load_config(LIVE_ROOT / "configs" / "sample-baskstream.yaml")
    assert yaml_cfg.site == "demo-lab"
    assert yaml_cfg.source == "baskstream"
    assert len(yaml_cfg.targets) == 2
    assert yaml_cfg.targets[0].ord_path.endswith("WEB_OA_T")
