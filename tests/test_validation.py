import sys
import types
from pathlib import Path
from rdflib import Graph, BNode, Literal
from rdflib.namespace import RDF, SH
from bask_223p.validate.shacl import shacl_validate, closure_from_directory

ROOT = Path(__file__).resolve().parents[1]


def test_shifty_api_called_safely_and_exports(tmp_path, monkeypatch):
    calls = {}
    def fake_validate(data, shapes, **opts):
        calls.update(opts)
        result = Graph()
        row = BNode()
        result.add((row, RDF.type, SH.ValidationResult))
        result.add((row, SH.resultMessage, Literal('Missing property')))
        return False, result, 'Fake validation finding'
    monkeypatch.setitem(sys.modules,'shifty', types.SimpleNamespace(validate=fake_validate))
    data = Graph().parse(str(ROOT/'examples/broken-site.ttl'))
    shapes = Graph().parse(str(ROOT/'examples/shapes/site-shapes.ttl'))
    result = shacl_validate(data, shapes, tmp_path)
    assert not result['conforms'] and result['results'] == 1
    assert calls['infer'] is False
    assert calls['on_unsupported'] == 'error'
    assert calls['graph_mode'] == 'data'
    assert (tmp_path/'validation-report.ttl').exists()
    assert 'Missing property' in (tmp_path/'violations.csv').read_text()


def test_reject_empty_shapes_before_engine(tmp_path):
    import pytest
    with pytest.raises(ValueError, match='no SHACL'):
        shacl_validate(Graph(), Graph(), tmp_path)


def test_ontoenv_contract_offline_and_imports(tmp_path, monkeypatch):
    calls = []
    class StubEnv:
        def __init__(self, **kwargs):
            calls.append(('init', kwargs))
        def __enter__(self): return self
        def __exit__(self,*args): return None
        def add(self, path, **kwargs):
            calls.append(('add', kwargs))
        def missing_imports(self, root): return []
        def copy_closure(self, root):
            return Graph().parse(str(ROOT/'examples/ontology-imports/base.ttl')), [root]
    monkeypatch.setitem(sys.modules, 'ontoenv', types.SimpleNamespace(OntoEnv=StubEnv))
    ttl = tmp_path / 'profile.ttl'
    ttl.write_text((ROOT/'examples/ontology-imports/profile.ttl').read_text())
    closure, names = closure_from_directory('https://example.org/fdd/validation/profile',tmp_path)
    assert len(closure) > 0 and len(names) == 1
    assert ('init', {'temporary': True, 'offline': True, 'strict': True}) in calls
    assert all('search_directories' not in kwargs for name, kwargs in calls if name == 'init')
    assert any(name == 'add' and kwargs.get('fetch_imports') is False for name, kwargs in calls)
