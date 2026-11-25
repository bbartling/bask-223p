import csv
from bask_223p.model.fqpn import parse_fqpn, parse_csv


def test_normal_name_parsed():
    result = parse_fqpn('LAB_Campus_B1650_HVAC_AHU-5_SA-T')
    assert result['Problems'] == ''
    assert result['Building'] == 'B1650'
    assert result['Point_meaning'] == 'Supply Air Temperature'


def test_domain_before_building():
    result = parse_fqpn('LAB_Campus_HVAC_B1650_AHU-5_OA-DMPR-C')
    assert result['Building'] == 'B1650'
    assert result['System'] == 'AHU-5'
    assert not result['Problems']


def test_handles_malformed_with_warnings():
    result = parse_fqpn('SA-T')
    assert result['Problems']
    assert 'unknown' in parse_fqpn('LAB_Campus_B12_HVAC_AHU-1_QW-LOL')['Problems']


def test_duplicates_marked(tmp_path):
    csvfile = tmp_path / 'in.csv'
    csvfile.write_text('FQPN\nLAB_Campus_B1650_HVAC_AHU-5_SA-T\nLAB_Campus_B1650_HVAC_AHU-5_SA-T\n')
    out = tmp_path / 'out.csv'
    result = parse_csv(csvfile, out)
    assert result['rows'] == 2 and result['flagged'] == 2
    with out.open() as stream:
        assert all('duplicate' in x['Problems'] for x in csv.DictReader(stream))


def test_empty_csv_preserves_header(tmp_path):
    src = tmp_path / 'names.csv'
    src.write_text('FQPN\n')
    dst = tmp_path / 'parsed.csv'
    assert parse_csv(src, dst)['rows'] == 0
    assert 'FQPN' in dst.read_text()
