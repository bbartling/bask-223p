import json
from pathlib import Path
from bask_223p.cli import main, parse_catalogs

ROOT = Path(__file__).resolve().parents[1]


def test_cli_build_inspect_query(tmp_path):
    output = tmp_path / 'model.ttl'
    assert main(['build','--csv',str(ROOT/'examples/bacnet_points.csv'),
                 '--base','https://example.org/demo','--out',str(output)]) == 0
    assert output.exists()
    assert main(['inspect', '--data',str(output),'--out',str(tmp_path/'info.json')]) == 0
    assert json.loads((tmp_path/'info.json').read_text())['triples'] > 10
    result = tmp_path / 'query.csv'
    assert main(['query','--data',str(output), '--sparql',str(ROOT/'examples/queries/bacnet_points.rq'),
                 '--out',str(result)]) == 0
    assert len(result.read_text().splitlines()) == 5


def test_cli_bad_catalog():
    import pytest
    with pytest.raises(ValueError, match='Name=path'):
        parse_catalogs(['oops'])


def test_cli_missing_shapes_is_error(tmp_path):
    assert main(['validate','--data',str(ROOT/'examples/broken-site.ttl')]) == 2


def test_cli_bad_file_is_error():
    assert main(['inspect','--data','does_not_exist.ttl']) == 2
