"""The fleet roster over a real authenticated authority (real HTTP, real SQLite store)."""
import hashlib
from io import BytesIO
import json
from threading import Thread
import urllib.error
import urllib.request

import pytest

from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore


def _host(available=10, reserve=1):
    psi = {k: dict(full_avg10=0.0, full_avg60=0.0, some_avg10=0.0, some_avg60=0.0) for k in ('cpu', 'io', 'memory')}
    return dict(attempts={}, memory_available_bytes=available, memory_reserve_bytes=reserve,
                memory_total_bytes=100, psi=psi, services={}, swap_in_bytes_per_second=0.0)


def _report(handlers, **extra):
    host = _host()
    return dict(boot='b', report=dict(capacity={'cpu': 4, 'memory_bytes': 8}, available={'cpu': 4, 'memory_bytes': 8},
                labels=extra.get('labels', {}), handlers=handlers, ready=True, host=extra.get('host', host)))


@pytest.fixture
def api(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'a.v1', 'b.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('caller', 'c'*32, 'caller', ('a.v1',)),
        Principal('w-full', 'f'*32, 'worker', worker='w-full', host='h1'),
        Principal('w-part', 'p'*32, 'worker', worker='w-part', host='h1'),
        Principal('w-drain', 'd'*32, 'worker', worker='w-drain', host='h2', claim_enabled=False),
        Principal('w-new', 'n'*32, 'worker', worker='w-new', host='h3'),
    ])
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, data=None, token='c'*32):
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/workloads/{path}',
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    yield call, store, server
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_roster_merges_config_registration_and_running_and_names_disagreements(api):
    call, store, server = api
    call('worker/report', _report(['a.v1', 'b.v1'], labels={'x_handler_release': 'aaaa'}), token='f'*32)
    call('worker/report', _report(['a.v1'], labels={'x_handler_release': 'bbbb'}), token='p'*32)
    call('worker/report', _report(['a.v1'], host=_host(0, 5)), token='d'*32)
    data = b'input'
    digest = hashlib.sha256(data).hexdigest()
    server.blobs.put('caller', digest, len(data), BytesIO(data))
    _, job = call('jobs', dict(version=1, key='k', handler='a.v1', input_digest=digest, need={'cpu': 1}))
    _, claim = call('worker/claim', {'boot': 'b'}, token='f'*32)
    assert claim['assignment']['job_id'] == job['id']
    status, roster = call('workers')
    assert status == 200
    workers = {w['id']: w for w in roster['workers']}
    assert set(workers) == {'w-full', 'w-part', 'w-drain', 'w-new'}
    assert workers['w-full']['connected'] and workers['w-full']['state'] == 'running'
    assert workers['w-full']['running'][0]['job_id'] == job['id'] and workers['w-full']['running'][0]['handler'] == 'a.v1'
    assert workers['w-part']['eligible'] and workers['w-part']['state'] == 'idle'
    assert workers['w-new']['registered'] is False and workers['w-new']['ineligible_reasons'] == ['never_registered']
    assert workers['w-drain']['claim_enabled'] is False
    assert 'draining: claim_enabled=false' in workers['w-drain']['ineligible_reasons']
    assert 'memory_below_host_reserve' in workers['w-drain']['ineligible_reasons']
    kinds = {(d['worker'], d['kind']) for d in roster['disagreements']}
    assert ('w-new', 'configured_never_registered') in kinds
    assert ('w-part', 'fewer_handlers_than_peer') in kinds
    assert any(k == 'handler_release_skew' for _, k in kinds)
    assert 'token' not in json.dumps(roster)


def test_roster_marks_a_silent_worker_offline_and_is_not_readable_by_a_worker(api):
    call, store, server = api
    call('worker/report', _report(['a.v1']), token='f'*32)
    with store.transaction() as db:
        db.execute("UPDATE workers SET seen=seen-1000 WHERE id='w-full'")
    _, roster = call('workers')
    entry = next(w for w in roster['workers'] if w['id'] == 'w-full')
    assert entry['state'] == 'offline' and not entry['eligible'] and entry['last_seen_age_s'] >= 1000
    assert ('w-full', 'configured_silent') in {(d['worker'], d['kind']) for d in roster['disagreements']}
    assert call('workers', token='f'*32)[0] == 403
    assert call('workers', token='bad')[0] == 401
