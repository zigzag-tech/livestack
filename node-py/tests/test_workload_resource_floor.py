"""The optional resource floor: schema, boundary, strict mode, and a real reload over HTTP.

openspec/changes/measured-resource-declarations tasks 3.1-3.2.
"""
import json
import sqlite3
from threading import Thread

import pytest

from livestack_node.workloads import resource_history
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.config import ReloadableConfig, load_config
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.service import reload_principals
from livestack_node.workloads.store import WorkloadStore

GIB = 1024**3
H = 'compile.v1'
TOKEN = 't'*32
PRINCIPALS = [dict(id='alice', token='a'*32, role='caller', handlers=[H, 'other.v1'])]


def seed(store, peaks, handler=H, need=8*GIB):
    with store.transaction() as db:
        for i, peak in enumerate(peaks):
            resource_history.record(db, handler, f'{handler}-{i}', {'resources': {'memory_peak_bytes': peak}},
                                    'succeeded', {'memory_bytes': need}, 1000.0+i)


def submit(store, need, handler=H, key='k'):
    return store.submit('alice', dict(version=1, key=key, handler=handler, input_digest='a'*64,
                                      need=dict(cpu=1, memory_bytes=need)))


@pytest.fixture
def store(tmp_path):
    s = WorkloadStore(tmp_path/'a.db', handlers={H, 'other.v1', 'compile.v2'}, clock=lambda: 2000.0)
    return s


def test_floor_refuses_one_byte_below_observed_times_margin_and_admits_at_it(store):
    store.set_resource_floor(dict(margin=1.5, min_samples=5, handlers=[H]))
    seed(store, [4*GIB]*4 + [8*GIB])  # max 8 GiB -> floor 12 GiB exactly
    with pytest.raises(WorkloadError) as error:
        submit(store, 12*GIB-1)
    assert error.value.status == 422 and 'resource_floor: need.memory_bytes' in str(error.value)
    assert f'floor {12*GIB}' in str(error.value) and 'observed max' in str(error.value)
    assert submit(store, 12*GIB, key='ok')['state'] == 'queued'


def test_floor_is_off_when_absent_and_never_raised_by_too_few_samples_or_uncovered_handlers(store):
    seed(store, [8*GIB]*5)
    assert submit(store, 1*GIB)['state'] == 'queued'  # no section: no floor
    store.set_resource_floor(dict(margin=1.15, min_samples=5, handlers=['compile.*']))
    seed(store, [8*GIB]*4, handler='compile.v2')
    assert submit(store, 1*GIB, handler='compile.v2', key='few')['state'] == 'queued'  # 4 < 5 samples
    seed(store, [8*GIB]*5, handler='other.v1')
    assert submit(store, 1*GIB, handler='other.v1', key='unc')['state'] == 'queued'    # not covered
    with pytest.raises(WorkloadError):
        submit(store, 1*GIB, key='covered')                                              # prefix match


def test_unreadable_history_disables_the_floor_and_says_so_but_strict_refuses_by_name(store, monkeypatch):
    seed(store, [8*GIB]*5)
    boom = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError('x'))
    monkeypatch.setattr(resource_history, 'summary', boom)
    store.set_resource_floor(dict(handlers=[H]))
    assert submit(store, 1*GIB)['state'] == 'queued'
    assert store.resource_floor_unavailable == 'OperationalError'
    monkeypatch.undo()
    monkeypatch.setattr(resource_history, 'summary', boom)
    store.set_resource_floor(dict(handlers=[H], strict=True))
    with pytest.raises(WorkloadError, match='resource_floor_unavailable') as error:
        submit(store, 1*GIB, key='strict')
    assert error.value.status == 503


@pytest.mark.parametrize('floor', [
    dict(handlers=['a.v1'], marginn=1.2), dict(handlers=['a.v1'], margin=0.9), dict(handlers=['a.v1'], min_samples=2),
    dict(handlers=['a.*b']), dict(handlers=['.*']), dict(handlers=[]), dict(handlers=['a.v1'], dimensions=['disk_bytes']),
    dict(handlers=['a.v1'], history_max_age_seconds=60), dict(margin=1.2)])
def test_bad_floor_sections_fail_closed_without_echoing_values(tmp_path, floor):
    path = tmp_path/'c.json'
    path.write_text(json.dumps(dict(principals=PRINCIPALS, handlers=[H], resource_floor=dict(floor, secret=TOKEN))))
    with pytest.raises(ValueError) as error:
        load_config(path, ReloadableConfig)
    assert TOKEN not in str(error.value)


def test_reload_turns_the_floor_on_over_http_and_a_bad_reload_keeps_the_previous_one(tmp_path):
    config = tmp_path/'authority.json'
    base = dict(principals=PRINCIPALS, handlers=[H])
    config.write_text(json.dumps(base))
    store = WorkloadStore(tmp_path/'a.db', handlers={H}, clock=lambda: 2000.0)
    server = WorkloadServer(('127.0.0.1', 0), store, [Principal(**p) for p in PRINCIPALS])
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        from hashlib import sha256
        from io import BytesIO
        digest = sha256(b'x').hexdigest()
        server.blobs.put('alice', digest, 1, BytesIO(b'x'))
        client = WorkloadClient(f'http://127.0.0.1:{server.server_port}', 'a'*32, timeout=5)
        seed(store, [8*GIB]*5)
        body = lambda key: dict(version=1, key=key, handler=H, input_digest=digest, need=dict(cpu=1, memory_bytes=GIB))
        assert client.submit(body('before'))['state'] == 'queued'
        config.write_text(json.dumps(dict(base, resource_floor=dict(handlers=[H]))))
        assert reload_principals(server, config, attempts=1, pause=0)
        with pytest.raises(WorkloadError) as error:
            client.submit(body('after'))
        assert error.value.status == 422
        config.write_text(json.dumps(dict(base, resource_floor=dict(handlers=[H], margin=0.5))))
        assert not reload_principals(server, config, attempts=1, pause=0)  # refused: floor stays on
        with pytest.raises(WorkloadError):
            client.submit(body('still'))
        config.write_text(json.dumps(base))
        assert reload_principals(server, config, attempts=1, pause=0)
        assert client.submit(body('off'))['state'] == 'queued'
    finally:
        server.shutdown()
        server.server_close()
