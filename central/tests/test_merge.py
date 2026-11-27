"""Merge, blank-node skolemization, and reconcile on small graphs."""
from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from central.merge import merge_models, skolemize, stable_nt
from central.reports import reconcile_edges

S223 = "http://data.ashrae.org/standard223#"
BACNET = "http://data.ashrae.org/bacnet/2020#"
REF = "https://brickschema.org/schema/Brick/ref#"
QUDT = "http://qudt.org/schema/qudt/hasUnit"
UNIT = "http://qudt.org/vocab/unit/"


def _edge(name: str, unit: str, *, extra: str = "") -> bytes:
    ttl = f"""
@prefix s223: <{S223}> .
@prefix bacnet: <{BACNET}> .
@prefix ref: <{REF}> .
@prefix qudt: <http://qudt.org/schema/qudt/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix unit: <{UNIT}> .

<https://example.org/bench/point/sa> a s223:Property ;
    rdfs:label "Supply air temperature" ;
    qudt:hasUnit unit:{unit} ;
    s223:hasExternalReference <https://example.org/bench/bacnet-ref/sa> .

<https://example.org/bench/bacnet-ref/sa> a s223:BACnetExternalReference ;
    bacnet:object-identifier "analog-input,1" ;
    bacnet:device-identifier "device,5007" ;
    bacnet:object-name "{name}" ;
    ref:BACnetURI "bacnet://5007/analog-input,1" .

<bacnet://5007> a bacnet:Device .
{extra}
"""
    return ttl.encode()


def test_identical_iris_collapse_and_conflicts_both_remain():
    left = _edge("SA-T", "DEG_C")
    right = _edge("SA-T", "DEG_F")
    merged = merge_models([("bench", "pi-bacnet", left), ("bench", "pi-niagara", right)])
    assert len(merged.union) < len(merged.named[0][2]) + len(merged.named[1][2]) + 6
    units = set(merged.union.objects(URIRef("https://example.org/bench/point/sa"), URIRef(QUDT)))
    assert units == {
        URIRef(UNIT + "DEG_C"),
        URIRef(UNIT + "DEG_F"),
    }
    iri = URIRef("urn:central:graph:bench:pi-bacnet")
    context = merged.dataset.graph(iri)
    assert (iri, RDF.type, URIRef("urn:central:EdgeGraph")) in context
    assert (iri, URIRef("urn:central:site"), Literal("bench")) in context
    report = reconcile_edges(merged)
    assert report["pairs"][0]["shared_bacnet_refs"] == 1
    assert report["pairs"][0]["conflicting"] == 1
    assert report["pairs"][0]["rows"][0]["kind"] == "conflicting"
    assert report["pairs"][0]["rows"][0]["unit_differs"] is True


def test_naive_turtle_concat_collides_blank_nodes_and_skolem_does_not():
    one = '@prefix ex: <urn:ex:> .\n_:b0 a ex:Note ; ex:from "one" .\n'
    two = '@prefix ex: <urn:ex:> .\n_:b0 a ex:Note ; ex:from "two" .\n'
    naive = Graph()
    naive.parse(data=one + two, format="turtle")
    notes = list(naive.subjects(RDF.type, URIRef("urn:ex:Note")))
    assert len(notes) == 1
    merged = merge_models([("bench", "pi-bacnet", one.encode()), ("bench", "pi-niagara", two.encode())])
    skolem = list(merged.union.subjects(RDF.type, URIRef("urn:ex:Note")))
    assert len(skolem) == 2
    assert all(str(node).startswith("urn:central:bnode:") for node in skolem)
    assert "pi-bacnet" in str(skolem[0]) + str(skolem[1])
    assert "pi-niagara" in str(skolem[0]) + str(skolem[1])
    again = skolemize(Graph().parse(data=one, format="turtle"), "bench", "pi-bacnet")
    assert stable_nt(again) == stable_nt(skolemize(Graph().parse(data=one, format="turtle"), "bench", "pi-bacnet"))


def test_edges_with_no_shared_bacnet_ref_are_not_paired():
    other = b"""
@prefix s223: <http://data.ashrae.org/standard223#> .
@prefix bacnet: <http://data.ashrae.org/bacnet/2020#> .
@prefix ref: <https://brickschema.org/schema/Brick/ref#> .
<https://example.org/bench/point/other> a s223:Property ;
    s223:hasExternalReference <https://example.org/bench/bacnet-ref/other> .
<https://example.org/bench/bacnet-ref/other> a s223:BACnetExternalReference ;
    bacnet:object-identifier "analog-input,9" ;
    bacnet:device-identifier "device,9" ;
    ref:BACnetURI "bacnet://9/analog-input,9" .
"""
    merged = merge_models([("bench", "pi-bacnet", _edge("SA-T", "DEG_C")), ("bench", "other", other)])
    assert reconcile_edges(merged)["pairs"] == []
