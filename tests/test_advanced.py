"""Contract tests for documented Shifty interfaces; work offline without Rust wheels."""
import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rdflib import Graph, Literal, Namespace
from bask_223p.validate.advanced import infer, explain, shape_bindings, batch_validate
from bask_223p.cli import main

EX = Namespace('https://example.org/')
ROOT = Path(__file__).resolve().parents[1]


def test_infer_does_not_mutate_input_and_writes_delta(tmp_path, monkeypatch):
    original = Graph().parse(data='@prefix ex: <https://example.org/> . ex:a ex:p ex:b .', format='turtle')
    shapes = Graph().parse(str(ROOT/'examples/shapes/site-shapes.ttl'))
    def fake_infer(data, shp, *, in_place=False, on_unsupported='error', max_iterations=1000, **kwargs):
        # Signature still accepts max_iterations so older engines keep receiving it.
        assert in_place is False and max_iterations == 12
        def full():
            copy = Graph()
            copy += data
            copy.add((EX.a, EX.derived, Literal('yes')))
            return copy
        return SimpleNamespace(graph=full, inferred_count=1,
            inferred_ntriples='<https://example.org/a> <https://example.org/derived> "yes" .\n',
            diagnostics=[])
    monkeypatch.setitem(sys.modules, 'shifty', SimpleNamespace(infer=fake_infer))
    result = infer(original, shapes, tmp_path, max_iterations=12)
    assert result['inferred_triples'] == 1
    assert len(original) == 1
    assert len(Graph().parse(tmp_path/'inferred-model.ttl')) == 2
    assert 'derived' in (tmp_path/'inferred-delta.nt').read_text()


def test_infer_iteration_limit_is_checked(tmp_path):
    with pytest.raises(ValueError, match='max_iterations'):
        infer(Graph(), Graph(), tmp_path, max_iterations=0)


def test_explain_reports_failure_and_unselected(tmp_path, monkeypatch):
    def fake_validate(**kwargs):
        assert kwargs['minimum_severity'] == 'violation'
        result = SimpleNamespace(statements=[
            SimpleNamespace(selector='class(ex:Point)', selected_foci=[
                SimpleNamespace(status='fail', focus=EX.missing,
                    evidence=SimpleNamespace(explain=lambda: 'count low')),
                SimpleNamespace(status='pass', focus=EX.valid,
                    evidence=SimpleNamespace(explain=lambda: 'all conditions pass'))]),
            SimpleNamespace(selector='class(ex:Unused)', selected_foci=[]),
        ])
        return result
    class FakeSession:
        def __init__(self, **kwargs):
            assert kwargs['infer'] is False and kwargs['graph_mode'] == 'data'
        validate = staticmethod(fake_validate)
    monkeypatch.setitem(sys.modules, 'shifty', SimpleNamespace(EvidenceSession=FakeSession))
    result = explain(Graph(), Graph(), tmp_path)
    rows = json.loads((tmp_path/'evidence.json').read_text())
    assert len(rows) == 1 and rows[0]['status'] == 'fail'
    assert result['unselected_targets'] == ['class(ex:Unused)']


def test_shape_bindings_retains_failed_partial_matches(tmp_path, monkeypatch):
    def binding(name, values):
        return SimpleNamespace(name=name, values=values)
    class FakeTerm:
        def n3(self): return '<https://example.org/point>'
    class FakeMap:
        focus = EX.vav
        shape_name = str(EX.profile)
        conforms = False
        successful = [('role/supply-air-temp', binding('SAT', [FakeTerm()]))]
        unsuccessful = [('role/flow', binding('Flow', None))]
        def value_map(self, *, by, python):
            assert by == 'name' and python is True
            return {'SAT': ['https://example.org/point']}
    def fake_shape_map(data, shapes, **kwargs):
        assert kwargs == {'infer':False, 'graph_mode':'data'}
        return [FakeMap()]
    monkeypatch.setitem(sys.modules, 'shifty', SimpleNamespace(shape_map=fake_shape_map))
    result = shape_bindings(Graph(), Graph(), tmp_path)
    assert result['focus_rows'] == 1
    rows = json.loads((tmp_path/'shape-map.json').read_text())
    assert rows[0]['conforms'] is False
    assert rows[0]['value_map']['SAT'][0].endswith('/point')
    assert any(not p['accepted'] for p in rows[0]['properties'])
    assert len(list(csv.DictReader((tmp_path/'bindings.csv').open()))) == 2
    assert shape_bindings(Graph(), Graph(), tmp_path, conforms_only=True)['focus_rows'] == 0


def test_batch_compiles_schema_only_once(tmp_path, monkeypatch):
    sources = [ROOT/'examples/broken-site.ttl', ROOT/'examples/ontology-imports/base.ttl']
    calls = []
    class FakePrepared:
        def __init__(self, shape): calls.append('compile'); self.diagnostics = []
        def validate(self, data, **kw):
            calls.append('validate')
            return False, Graph(), 'broken'
    monkeypatch.setitem(sys.modules, 'shifty', SimpleNamespace(PreparedValidator=FakePrepared))
    summary = batch_validate(sources, Graph(), tmp_path)
    assert calls == ['compile', 'validate', 'validate']
    assert summary['failed'] == 2
    assert len(list(tmp_path.glob('*-report.ttl'))) == 2
    assert main(['batch','--data',str(sources[0]), '--shapes',str(ROOT/'examples/shapes/site-shapes.ttl'),
                 '--out',str(tmp_path/'cli')]) == 1


def test_cli_inference_exports_using_fake_engine(tmp_path, monkeypatch):
    def fake_infer(data, shapes, **kwargs):
        return SimpleNamespace(graph=lambda: data, inferred_count=0,
             inferred_ntriples='', diagnostics=[])
    monkeypatch.setitem(sys.modules, 'shifty', SimpleNamespace(infer=fake_infer))
    assert main(['infer','--data',str(ROOT/'examples/broken-site.ttl'),
                 '--shapes',str(ROOT/'examples/shapes/site-shapes.ttl'),
                 '--out',str(tmp_path)]) == 0
    assert (tmp_path/'inference-summary.json').is_file()


def test_structured_algebra_findings_not_parsing_messages(tmp_path, monkeypatch):
    from bask_223p.validate.advanced import algebra_findings
    reason = SimpleNamespace(severity='Violation', constraint_kind='Cardinality',
        path='oc:hasPoint', value='ex:one', author_message='missing',
        message='fallback', constraint_id='42')
    violation = SimpleNamespace(focus_node='ex:one', shape_name='ex:Equipment', reasons=[reason])
    def fake(data, shapes, **kwargs):
        assert kwargs['infer'] is False and kwargs['on_unsupported'] == 'error'
        return SimpleNamespace(violations=[violation], conforms=False, diagnostics=[])
    monkeypatch.setitem(sys.modules, 'shifty', SimpleNamespace(validate_algebra=fake))
    result = algebra_findings(Graph(), Graph(), tmp_path)
    assert result['failure_reasons'] == 1 and not result['conforms']
    assert json.loads((tmp_path/'algebra-findings.json').read_text())[0]['message'] == 'missing'
