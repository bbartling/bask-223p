from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fake_baskstream_station import FakeBaskStreamStation  # noqa: E402

from live_ingest.api import create_app  # noqa: E402
from live_ingest.config import load_config  # noqa: E402
from live_ingest.models import PointTarget  # noqa: E402
from live_ingest.rdf_store import RdfKnowledgeBase  # noqa: E402
from live_ingest.sinks import DuckDbSink  # noqa: E402
from live_ingest.sources import BacnetRpmSource, BaskStreamSource, CompositeSource  # noqa: E402
from test_bacnet_rpm import _FakeTransport  # noqa: E402


def test_pyoxigraph_and_shacl_validation(tmp_path):
    cfg = load_config(REPO_ROOT / "examples" / "illustrative-ahu-refs.ttl", site="lab")
    kb = RdfKnowledgeBase(cfg.graph)
    assert kb.triple_count > 10
    res = kb.query("SELECT ?ref WHERE { ?x <https://brickschema.org/schema/Brick/ref#BACnetURI> ?ref } ORDER BY ?ref")
    assert res["engine"] == "pyoxigraph"
    assert res["count"] >= 5

    # Validate against a minimal valid SHACL shape (Shifty primary, pyshacl fallback)
    shapes = tmp_path / "shapes.ttl"
    shapes.write_text("""
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix s223: <http://data.ashrae.org/standard223#> .
@prefix ref: <https://brickschema.org/schema/Brick/ref#> .
<http://example.org/RefShape> a sh:NodeShape ;
  sh:targetClass s223:BACnetExternalReference ;
  sh:property [ sh:path ref:BACnetURI ; sh:minCount 1 ] .
""", encoding="utf-8")
    val = kb.validate(shapes_path=shapes)
    assert val["conforms"] is True
    assert val["engine"] in {"shifty", "pyshacl-fallback"}


def test_unified_fastapi_playground_duckdb_sparql_join_and_both_sources(tmp_path):
    cfg = load_config(REPO_ROOT / "examples" / "illustrative-ahu-refs.ttl", site="lab")
    tr = _FakeTransport()
    tr.values[("analog-value,1", "present-value")] = 55.0
    bac_src = BacnetRpmSource(
        [PointTarget(site="lab", device="10001", object_id="analog-value,1", units="DEG_F")],
        transport=tr,
    )

    with FakeBaskStreamStation() as st:
        bs_src = BaskStreamSource(
            station=st.url,
            username=st.user,
            password=st.password,
            site="lab",
        )
        both = CompositeSource([bs_src, bac_src])
        ddb = DuckDbSink(tmp_path / "app.duckdb")
        app = create_app(
            source=both,
            source_kind="both",
            targets=cfg.targets,
            duckdb_sink=ddb,
            graph=cfg.graph,
            site="lab",
        )
        with TestClient(app) as client:
            assert client.get("/playground").status_code == 200
            assert client.get("/docs").status_code == 200
            h = client.get("/health").json()
            assert h["source"] == "both"
            assert h["duckdb_enabled"] is True
            assert h["sparql_engine"] == "pyoxigraph"

            polled = client.post("/poll/once").json()
            assert polled["count"] == 10  # 9 baskstream + 1 bacnet-rpm

            hist = client.get("/history", params={"ref": "bacnet://10001/analog-value,1"}).json()
            assert hist["store"] == "duckdb"
            assert hist["count"] == 2  # both sources wrote this ref

            sql_ok = client.post("/api/sql", json={"sql": "SELECT source, COUNT(*) AS n FROM telemetry GROUP BY source ORDER BY source"})
            assert sql_ok.status_code == 200
            assert {r["source"] for r in sql_ok.json()["rows"]} == {"bacnet-rpm", "baskstream"}

            sql_bad = client.post("/api/sql", json={"sql": "DROP TABLE telemetry"})
            assert sql_bad.status_code == 400

            sp_bad = client.post("/api/sparql", json={"query": "INSERT DATA { <http://ex/a> <http://ex/b> 1 }"})
            assert sp_bad.status_code == 400

            joined = client.get("/api/join/example").json()
            assert joined["count"] >= 5
            assert any(r["sample_count"] == 2 for r in joined["rows"])

            assert client.get("/bacnet/who-is").json()["count"] == 1
            assert client.get("/bacnet/devices/10001/objects").json()["count"] == 2
            rm = client.post("/bacnet/read-multiple", json={"items": [{"device": 10001, "object_id": "analog-value,1"}]}).json()
            assert rm["samples"][0]["value"] == 55.0

            with client.websocket_connect("/live") as ws:
                snap = ws.receive_json()
                assert snap["type"] == "snapshot"
                assert snap["count"] >= 1


def test_ontoenv_local_import_closure(tmp_path):
    from rdflib import Graph

    ont_dir = tmp_path / "ont"
    ont_dir.mkdir()
    (ont_dir / "base.ttl").write_text("""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix ex: <http://example.org/ont/> .
<http://example.org/ont/base> a owl:Ontology .
ex:ThingA a owl:Class .
""", encoding="utf-8")
    model = Graph()
    model.parse(data="""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix ex: <http://example.org/ont/> .
<http://example.org/ont/site> a owl:Ontology ;
  owl:imports <http://example.org/ont/base> .
ex:item1 a ex:ThingA .
""", format="turtle")
    kb = RdfKnowledgeBase(model)
    info = kb.resolve_imports([ont_dir], offline=True)
    assert info["engine"] == "ontoenv"
    assert info["triples"] == 3
    assert kb.query("ASK { <http://example.org/ont/ThingA> a <http://www.w3.org/2002/07/owl#Class> }")["boolean"] is True
