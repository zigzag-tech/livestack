"""Real authenticated HTTP over a real durable authority, not a mocked server."""
import json
import hashlib
from io import BytesIO
from threading import Event, Thread
import urllib.error
import urllib.request

import pytest

from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.transfer import InputTransfer


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


def test_object_put_acknowledges_canonical_cas_before_optional_mirror(tmp_path):
    class BlockingMirror:
        def __init__(self):
            self.started, self.release, self.finished = Event(), Event(), Event()

        def put(self, _digest, _source, _max_bytes):
            self.started.set()
            assert self.release.wait(5)
            self.finished.set()

    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    mirror = BlockingMirror()
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('alice', 'a'*32, 'caller', ('test.v1',)),
    ], artifact_mirror=mirror)
    serving = Thread(target=server.serve_forever, daemon=True)
    serving.start()
    source = tmp_path/'input'
    source.write_bytes(b'canonical before cache')
    client = WorkloadClient(f'http://127.0.0.1:{server.server_port}', 'a'*32)
    result = {}
    uploading = Thread(target=lambda: result.update(receipt=InputTransfer(client).put(source)))
    uploading.start()
    try:
        assert mirror.started.wait(2)
        uploading.join(timeout=1)
        assert not uploading.is_alive(), 'optional mirror withheld the canonical CAS acknowledgement'
        assert result['receipt']['digest'] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert not mirror.finished.is_set()
    finally:
        mirror.release.set()
        uploading.join(timeout=5)
        assert mirror.finished.wait(5)
        server.shutdown()
        serving.join(timeout=5)
        server.server_close()


def test_version_two_inputs_require_owned_exact_objects(api):
    primary = hashlib.sha256(b'input fixture').hexdigest()
    missing = hashlib.sha256(b'missing').hexdigest()
    base = dict(version=2, key='multi', handler='test.v1', input_digest=primary,
                input_objects=[{'name':'web/web.tar.gz','digest':missing,'size':7}], need={'cpu':1})
    assert api('jobs', base)[0] == 404
    # The primary object exists for Alice but is not owned by Bob.
    assert api('jobs', dict(base, key='foreign', input_objects=[
        {'name':'copy','digest':primary,'size':len(b'input fixture')}]), token='b'*32)[0] == 404
    assert api('jobs', dict(base, key='wrong-size', input_objects=[
        {'name':'copy','digest':primary,'size':1}]))[0] == 400
    status, job = api('jobs', dict(base, input_objects=[
        {'name':'copy','digest':primary,'size':len(b'input fixture')}]))
    assert status == 200 and job['spec']['version'] == 2
    assert job['spec']['input_objects'][0]['name'] == 'copy'


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
