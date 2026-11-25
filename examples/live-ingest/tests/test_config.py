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
    from bask_223p.bacnet.refs import S223
    from rdflib.namespace import RDF

    cfg = load_config(REPO_ROOT / "examples" / "bench-scan.ttl", site="lab-1")
    assert cfg.graph is not None
    assert cfg.scan_graph is not None
    # cfg.graph is the single-emitter 223P graph; raw scan TTL sits in cfg.scan_graph sidecar
    assert len(list(cfg.graph.subjects(RDF.type, S223.BACnetExternalReference))) >= 4
    assert "urn:bask-223p:bacnet-scan" in cfg.sidecar_graphs()
    assert len(cfg.targets) >= 4
    assert all(t.ref.startswith("bacnet://") for t in cfg.targets)
    sat = next(t for t in cfg.targets if t.object_id == "analog-input,1" and t.device == "10001")
    assert sat.units == "DEG_F"


def test_load_csv_and_yaml():
    csv_cfg = load_config(LIVE_ROOT / "configs" / "sample-bacnet-points.csv")
    assert len(csv_cfg.targets) == 4
    assert csv_cfg.targets[0].ref == "bacnet://10001/analog-input,1"

    yaml_cfg = load_config(LIVE_ROOT / "configs" / "sample-baskstream.yaml")
    assert yaml_cfg.site == "demo-lab"
    assert yaml_cfg.source == "baskstream"
    assert len(yaml_cfg.targets) == 2
    assert yaml_cfg.targets[0].ord_path.endswith("WEB_OA_T")
