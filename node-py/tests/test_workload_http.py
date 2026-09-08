"""Real authenticated HTTP over a real durable authority, not a mocked server."""
import json
import hashlib
from io import BytesIO
from threading import Thread
import urllib.error
import urllib.request

import pytest

from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore


@pytest.fixture
def api(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1', 'sign.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('alice', 'a'*32, 'caller', ('test.v1',)),
        Principal('bob', 'b'*32, 'caller', ('test.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='w1', host='host1'),
    ])
    data = b'input fixture'
    server.blobs.put('alice', hashlib.sha256(data).hexdigest(), len(data), BytesIO(data))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def call(path, data=None, token='a'*32):
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/workloads/{path}',
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    yield call
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_auth_handler_allowlist_and_owner_isolation(api):
    spec = dict(version=1, key='request1', handler='test.v1', input_digest=hashlib.sha256(b'input fixture').hexdigest(), need={'cpu': 1})
    assert api('jobs', spec, token='bad')[0] == 401
    assert api('jobs', dict(spec, handler='sign.v1'))[0] == 403
    status, job = api('jobs', spec)
    assert status == 200
    assert api('jobs', spec)[1]['id'] == job['id']
    assert api('jobs/'+job['id'], token='b'*32)[0] == 404
    assert api('worker/claim', {'boot': 'boot'})[0] == 403


def test_worker_identity_is_bound_to_credential_and_result_is_fenced(api):
    spec = dict(version=1, key='request1', handler='test.v1', input_digest=hashlib.sha256(b'input fixture').hexdigest(), need={'cpu': 1})
    _, job = api('jobs', spec)
    token = 'w'*32
    report = dict(boot='boot', report=dict(capacity={'cpu': 2}, available={'cpu': 2},
                  labels={}, handlers=['test.v1'], ready=True))
    assert api('worker/report', dict(report, worker='spoofed'), token=token)[1]['worker'] == 'w1'
    _, out = api('worker/claim', {'boot': 'boot'}, token=token)
    a = out['assignment']
    assert a['job_id'] == job['id'] and a['worker'] == 'w1'
    completion = dict(boot='boot', attempt_id=a['attempt_id'], fence=a['fence'], input_digest=hashlib.sha256(b'input fixture').hexdigest(),
                      outcome='succeeded', result={'ok': True})
    assert api('worker/complete', dict(completion, fence=99), token=token)[0] == 409
    assert api('worker/complete', completion, token=token)[1]['state'] == 'succeeded'
    assert api('jobs/'+job['id'])[1]['result']['result'] == {'ok': True}
