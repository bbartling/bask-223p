"""Demonstrates the SPARQL -> bacnet:// ref -> DuckDB time-series join workflow."""
from __future__ import annotations

from pathlib import Path

from live_ingest.config import load_config
from live_ingest.models import PointSample, utc_now_iso
from live_ingest.rdf_store import RdfKnowledgeBase
from live_ingest.sinks import DuckDbSink

SPARQL = """
PREFIX s223: <http://data.ashrae.org/standard223#>
PREFIX ref: <https://brickschema.org/schema/Brick/ref#>
PREFIX qudt: <http://qudt.org/schema/qudt/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT DISTINCT ?point ?label ?ref ?unit WHERE {
  { ?point s223:hasExternalReference ?r } UNION { ?point ref:hasExternalReference ?r }
  ?r ref:BACnetURI ?ref .
  OPTIONAL { ?point rdfs:label ?label }
  OPTIONAL { ?point qudt:hasUnit ?unit }
}
ORDER BY ?ref
"""


def main() -> None:
    ttl_path = Path(__file__).resolve().parents[2] / "illustrative-ahu-refs.ttl"
    cfg = load_config(ttl_path, site="demo-lab")
    kb = RdfKnowledgeBase(cfg.graph)
    sp_res = kb.query(SPARQL)
    print(f"SPARQL ({sp_res['engine']}) found {sp_res['count']} bacnet:// refs")

    sink = DuckDbSink(":memory:")
    ts = utc_now_iso()
    samples = [
        PointSample(
            timestamp_utc=ts,
            site="demo-lab",
            source="demo-seed",
            device=t.device,
            object=t.object_id,
            property=t.property_id,
            value=55.0 + i,
            units=t.units,
            status="ok",
            ref=t.ref,
        )
        for i, t in enumerate(cfg.targets)
    ]
    sink.write(samples)

    refs = [r["ref"] for r in sp_res["rows"] if r.get("ref")]
    placeholders = ", ".join(["?"] * len(refs))
    sql = f"""
        SELECT ref, COUNT(*) AS n, ROUND(AVG(value_num), 2) AS avg_val, MAX(timestamp_utc) AS last_ts
        FROM telemetry
        WHERE ref IN ({placeholders})
        GROUP BY ref
        ORDER BY ref
    """
    cur = sink.conn.execute(sql, refs)
    stats = {row[0]: row[1:] for row in cur.fetchall()}
    for row in sp_res["rows"]:
        st = stats.get(row["ref"], (0, None, None))
        print(f"  {row.get('label') or row['point']} -> {row['ref']} | n={st[0]} avg={st[1]} last={st[2]}")
    sink.close()


if __name__ == "__main__":
    main()
