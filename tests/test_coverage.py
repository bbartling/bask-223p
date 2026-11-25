import csv
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, OWL
from bask_223p.audit.coverage import audit, label_index, find_matches, normalize


def test_normalize_case_hyphens():
    assert normalize('SupplyAir-Temperature') == 'supply air temperature'


def test_duplicate_labels_retained():
    g = Graph()
    for i in (1, 2):
        node = URIRef(f'https://example.com/{i}')
        g.add((node, RDF.type, OWL.Class))
        g.add((node, RDFS.label, Literal('Chiller')))
    matches = find_matches('Chiller', 'Sample', label_index(g))
    assert len(matches) == 2
    assert {m.uri for m in matches} == {'https://example.com/1', 'https://example.com/2'}
    assert all(m.status.endswith('_REVIEW') for m in matches)


def test_audit_generates_html_json_csv(tmp_path):
    requirements = tmp_path / 'terms.csv'
    requirements.write_text('Requirement\nChiller\nFOO_NOT_FOUND\n')
    ont = tmp_path / 'ont.ttl'
    ont.write_text('''@prefix ex: <https://example.com/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:Chiller a owl:Class; rdfs:label "Chiller" .''')
    output = tmp_path / 'audit'
    result = audit(requirements, {'test': ont}, output)
    assert result['terms_with_exact_label'] == 1
    assert result['not_semantic_validation']
    with (output / 'candidates.csv').open() as stream:
        assert list(csv.DictReader(stream))[0]['rdf_kind'] == 'class'
    assert (output / 'summary.json').exists()
    assert 'Not SHACL validation' in (output / 'report.html').read_text()


def test_bad_cutoff(tmp_path):
    import pytest
    with pytest.raises(ValueError, match='cutoff'):
        audit(tmp_path/'no.csv', {'X':tmp_path/'no.ttl'}, tmp_path/'out', cutoff=1.2)


def test_html_escapes_untrusted_values(tmp_path):
    from bask_223p.audit.report import write_html
    from bask_223p.audit.coverage import Candidate
    fake = Candidate('<script>alert(1)</script>', 'test', 1, 'REVIEW', '<b>foo</b>',
                     'https://example.org/a', 1, 'class')
    summary = {'terms': 1, 'terms_with_any_candidate': 1, 'terms_without_candidate': 0, 'terms_with_exact_label': 1}
    outfile = tmp_path / 'report.html'
    write_html([fake], summary, outfile)
    text = outfile.read_text()
    assert '&lt;script&gt;' in text and '<script>alert(1)</script>' not in text
