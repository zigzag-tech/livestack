"""Over real HTTP: 507 refusal, /retention/plan, status storage block and the roster flag."""
import hashlib
import json
import sys
import urllib.error
import urllib.request
from threading import Thread
from types import SimpleNamespace

import pytest

from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.retention_tiers import RetentionTiers
from livestack_node.workloads.storage_bounds import HeadroomGuard, StorageBounds
from livestack_node.workloads.store import WorkloadStore

sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from test_storage_headroom_worker import GIB, worker  # noqa: E402


@pytest.fixture
def api(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'native.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('root', 'r'*32, 'admin', ('native.v1',)), Principal('alice', 'a'*32, 'caller', ('native.v1',)),
        Principal('w1', 'w'*32, 'worker', worker='w1', host='h1')])
    free = [1000]
    fs = lambda path: SimpleNamespace(f_blocks=10_000, f_frsize=1, f_bavail=free[0])
    server.blobs.replace_policy(HeadroomGuard(server.blobs.root, server.blobs.max_bytes,
        StorageBounds.validate(dict(objects=dict(headroom_bytes=500))), statvfs=fs),
        RetentionTiers.validate(dict(jobs=dict(failed_seconds=14*86400))), 256)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def call(path, data=None, token='r'*32, raw=None):
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/workloads/{path}',
            data=raw if raw is not None else (json.dumps(data).encode() if data is not None else None),
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    yield call, server, free, tmp_path
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_status_plan_and_roster_surfaces(api):
    call, server, free, tmp_path = api
    status, body = call('status')
    assert status == 200 and body['storage']['bound']['state'] == 'ok'
    assert body['storage']['bound']['floor_bytes'] == 500
    status, plan = call('retention/plan')
    assert status == 200 and plan['jobs']['would_delete'] == 0 and 'unreferenced_blobs' in plan
    assert call('retention/plan', token='a'*32)[0] == 403
    free[0] = 400
    assert call('status')[1]['storage']['bound']['state'] == 'refusing'
    # a worker whose reserve exceeds its free space is flagged in the roster
    w = worker(tmp_path, 60*GIB, disk_reserve_bytes=64*GIB)
    server.store.register('w1', 'h1', 'boot', w.report())
    roster = call('workers')[1]
    entry = next(e for e in roster['workers'] if e['id'] == 'w1')
    assert entry['reserve_exceeds_free'] is True and entry['disk_unavailable']['reason'] == 'reserve_exceeds_free'
    assert any(x['kind'] == 'reserve_exceeds_free' for x in roster['warnings'])
    assert roster['storage']['state'] == 'refusing'


def test_object_put_below_the_floor_is_507_and_names_it(api):
    call, server, free, _ = api
    free[0] = 520
    data = b'x'*30
    digest = hashlib.sha256(data).hexdigest()
    server.blobs.put('alice', hashlib.sha256(b'ok').hexdigest(), 2, __import__('io').BytesIO(b'ok'))
    from livestack_node.workloads.model import WorkloadError
    with pytest.raises(WorkloadError, match='storage_headroom') as refused:
        server.blobs.put('alice', digest, len(data), __import__('io').BytesIO(data))
    assert refused.value.status == 507
