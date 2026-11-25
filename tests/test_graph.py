import pytest
from pathlib import Path
from rdflib import URIRef
from rdflib.namespace import RDF
from bask_223p.model.graph import build_site_graph, VOCAB, load_approved_mappings, uri_for

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / 'examples' / 'bacnet_points.csv'


def test_graph_builds_topology():
    graph, summary = build_site_graph(SAMPLE, 'https://example.com/demo')
    assert summary['points'] == 4
    assert summary['buildings'] == 2
    assert summary['equipment'] == 3  # AHU-5, AHU-1 and its VAV-3 child
    assert summary['warnings'] == []
    assert len(set(graph.subjects(RDF.type, VOCAB.AddressedPoint))) == 4
    assert len(list(graph.triples((None, VOCAB.hasPoint, None)))) == 4
    assert len(list(graph.triples((None, VOCAB.hasSubEquipment, None)))) == 1
    assert len(list(graph.triples((None, VOCAB.hasBuilding, None)))) == 2
    assert summary['approved_class_assignments'] == 0


def test_graph_uri_stable_and_unique():
    assert uri_for('https://x.org', 'point', 'one') == uri_for('https://x.org', 'point', 'one')
    assert uri_for('https://x.org', 'point', 'one') != uri_for('https://x.org', 'point', 'two')
    assert uri_for('https://x.org', 'point', 'a b') != uri_for('https://x.org', 'point', 'a-b')


def test_reject_duplicate_fqpn(tmp_path):
    csvfile = tmp_path / 'x.csv'
    text = SAMPLE.read_text().splitlines()
    csvfile.write_text('\n'.join([text[0], text[1], text[1]]))
    with pytest.raises(ValueError, match='Duplicate FQPN'):
        build_site_graph(csvfile, 'https://example.org')


def test_incomplete_bacnet_is_not_masked(tmp_path):
    p = tmp_path / 'x.csv'
    p.write_text('FQPN,device_instance,object_type\nLAB_Campus_B1650_HVAC_AHU-1_SA-T,1005,analogInput\n')
    graph, summary = build_site_graph(p, 'https://example.org')
    assert len(set(graph.subjects(RDF.type, VOCAB.AddressedPoint))) == 1
    assert not list(graph.objects(None, VOCAB.object_instance))


def test_strict_names_reject_malformed(tmp_path):
    p = tmp_path / 'x.csv'
    p.write_text('FQPN\nSA-T\n')
    with pytest.raises(ValueError, match='expected'):
        build_site_graph(p, 'https://example.org', strict_names=True)


def test_invalid_bacnet_integer(tmp_path):
    p = tmp_path / 'x.csv'
    p.write_text('FQPN,device_instance\nLAB_Campus_B1650_HVAC_AHU-1_SA-T,-1\n')
    with pytest.raises(ValueError, match='invalid BACnet'):
        build_site_graph(p, 'https://example.org')


def test_approved_mapping_controls_external_type(tmp_path):
    p = tmp_path / 'review.csv'
    name = 'LAB_Campus_B1650_HVAC_AHU-5_SA-T'
    p.write_text('FQPN,ClassURI,Status\n'+name+',https://example.org/verified/A,review\n')
    g, summary = build_site_graph(SAMPLE, 'https://example.com', mappings=p)
    assert summary['approved_class_assignments'] == 0
    p.write_text('FQPN,ClassURI,Status\n'+name+',https://example.org/verified/A,approved\n')
    g, summary = build_site_graph(SAMPLE, 'https://example.com', mappings=p)
    assert summary['approved_class_assignments'] == 1
    assert list(g.subjects(RDF.type, URIRef('https://example.org/verified/A')))


def test_invalid_class_uri_rejected(tmp_path):
    mapping = tmp_path / 'mapping.csv'
    mapping.write_text('FQPN,ClassURI,Status\nabc,not-a-uri,approved\n')
    with pytest.raises(ValueError, match='Invalid approved'):
        load_approved_mappings(mapping)


def test_unknown_approved_mapping_rejected(tmp_path):
    mapping = tmp_path / 'mapping.csv'
    mapping.write_text('FQPN,ClassURI,Status\nunknown,https://x.org/point,approved\n')
    with pytest.raises(ValueError, match='unknown point'):
        build_site_graph(SAMPLE, 'https://x.org', mappings=mapping)


def test_reject_duplicate_bacnet_object_in_two_points(tmp_path):
    p = tmp_path / 'points.csv'
    p.write_text('FQPN,device_instance,object_type,object_instance\n'
        'LAB_Demo_B11_HVAC_AHU-1_SA-T,123,analogInput,5\n'
        'LAB_Demo_B11_HVAC_AHU-1_RA-T,123,analogInput,5\n')
    with pytest.raises(ValueError, match='Duplicate BACnet object'):
        build_site_graph(p, 'https://example.org')
