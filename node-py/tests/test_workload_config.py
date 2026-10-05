"""The authority config is schema-validated and fails closed (no env vars)."""
import json

import pytest

from livestack_node.workloads.config import load_config
from livestack_node.workloads.service import load_principals

TOKEN = 't'*32
BASE = dict(state_dir='/tmp/x', handlers=['test.v1'],
            principals=[dict(id='zzops', token=TOKEN, role='caller', handlers=['test.v1'], upload_grants=True)])


def write(tmp_path, **override):
    path = tmp_path/'authority.json'
    path.write_text(json.dumps({**BASE, **override}))
    return path


def test_valid_config_round_trips_and_keeps_upload_grants_flag(tmp_path):
    config = load_config(write(tmp_path, public_base_url='https://hub.example:8810/', port=8810))
    assert config['public_base_url'] == 'https://hub.example:8810' and 'bind' not in config
    assert load_principals(write(tmp_path))[0].upload_grants is True
    full = write(tmp_path)  # reload judges only principals; startup judges the whole file
    full.write_text(json.dumps(dict(principals=BASE['principals'], surprise=1)))
    assert load_principals(full)[0].id == 'zzops'
    with pytest.raises(ValueError):
        load_config(full)


@pytest.mark.parametrize('override', [
    dict(surprise=1),                                    # unknown top-level key
    dict(public_base_url='hub.example:8810'),            # not an http(s) origin
    dict(public_base_url='http://hub/path'),
    dict(public_base_url='http://u:p@hub'),
    dict(port='8810'), dict(port=70000), dict(bind=''),  # strict types / ranges
    dict(blob_limits={'max_byte': 1}),                   # misspelt bound
    dict(blob_limits={'max_bytes': 0}),
    dict(handlers='test.v1'),
])
def test_invalid_config_is_refused_without_echoing_secrets(tmp_path, override):
    with pytest.raises(ValueError) as error:
        load_config(write(tmp_path, **override))
    assert TOKEN not in str(error.value)


def test_unknown_principal_key_and_bad_json_fail_closed(tmp_path):
    bad = dict(BASE, principals=[dict(BASE['principals'][0], upload_grant=True)])
    path = tmp_path/'a.json'
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        load_principals(path)
    path.write_text('{')
    with pytest.raises(ValueError):
        load_config(path)
    with pytest.raises(ValueError):
        load_config(tmp_path/'missing.json')
