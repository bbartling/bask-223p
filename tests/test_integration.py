"""Actual external engine calls: skipped if packages are unavailable locally.

Run with `uv run --extra all pytest tests/test_integration.py -q` for full coverage.
"""
from pathlib import Path
import pytest
from rdflib import Graph
from rdflib.namespace import SH, RDF
from bask_223p.validate.shacl import shacl_validate, closure_from_directory
from bask_223p.model.graph import build_site_graph

ROOT = Path(__file__).resolve().parents[1]


def test_actual_shifty_detects_broken_graph(tmp_path):
    pytest.importorskip('shifty')
    data = Graph().parse(str(ROOT/'examples/broken-site.ttl'))
    shape = Graph().parse(str(ROOT/'examples/shapes/site-shapes.ttl'))
    verdict = shacl_validate(data, shape, tmp_path)
    assert verdict['conforms'] is False
    assert verdict['results'] >= 1


def test_actual_shifty_accepts_valid_model(tmp_path):
    pytest.importorskip('shifty')
    data, _ = build_site_graph(ROOT/'examples/bacnet_points.csv', 'https://example.org/test')
    shape = Graph().parse(str(ROOT/'examples/shapes/site-shapes.ttl'))
    verdict = shacl_validate(data, shape, tmp_path)
    assert verdict['conforms'] is True
    assert verdict['results'] == 0


def test_actual_ontoenv_local_import_closure():
    pytest.importorskip('ontoenv')
    graph, names = closure_from_directory('https://example.org/fdd/validation/profile',
                                         ROOT/'examples/ontology-imports')
    assert len(names) >= 2
    assert set(graph.subjects(RDF.type, SH.NodeShape))


def test_combined_imports_shifty(tmp_path):
    pytest.importorskip('ontoenv')
    pytest.importorskip('shifty')
    shapes, names = closure_from_directory('https://example.org/fdd/validation/profile',
                                         ROOT/'examples/ontology-imports')
    data = Graph().parse(str(ROOT/'examples/broken-site.ttl'))
    verdict = shacl_validate(data, shapes, tmp_path)
    assert verdict['conforms'] is False


def test_actual_shifty_inference_fixed_point(tmp_path):
    pytest.importorskip('shifty')
    from bask_223p.validate.advanced import infer
    data, _ = build_site_graph(ROOT/'examples/bacnet_points.csv', 'https://example.org/inf')
    rules = Graph().parse(str(ROOT/'examples/rules/local-rollup.ttl'))
    result = infer(data, rules, tmp_path)
    assert result['inferred_triples'] >= 1
    assert (tmp_path/'inferred-model.ttl').exists()


def test_actual_shifty_profile_bindings(tmp_path):
    pytest.importorskip('shifty')
    from bask_223p.validate.advanced import shape_bindings
    data, _ = build_site_graph(ROOT/'examples/bacnet_points.csv', 'https://example.org/bind')
    profile = Graph().parse(str(ROOT/'examples/shapes/point-binding-profile.ttl'))
    result = shape_bindings(data, profile, tmp_path)
    assert result['focus_rows'] >= 2
    assert (tmp_path/'shape-map.json').exists()


def test_actual_shifty_evidence(tmp_path):
    pytest.importorskip('shifty')
    from bask_223p.validate.advanced import explain
    data = Graph().parse(str(ROOT/'examples/broken-site.ttl'))
    profile = Graph().parse(str(ROOT/'examples/shapes/site-shapes.ttl'))
    result = explain(data, profile, tmp_path)
    assert result['emitted_focus_evidence'] >= 1
