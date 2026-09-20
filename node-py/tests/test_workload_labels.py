"""J.1: labels on submission, the reserved labels.owner, delegate_prefix authz."""
import hashlib
from io import BytesIO
import json
from threading import Thread
import urllib.error
import urllib.request

import pytest

from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore


@pytest.fixture
def api(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={
        'attune.produce_item', 'benchday.thumbnail', 'unchain.render_chunk'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('attune-hub', 'a'*32, 'caller', ('attune.produce_item',),
                  delegate_prefix='attune:'),
        Principal('benchday-hub', 'b'*32, 'caller', ('benchday.thumbnail',),
                  delegate_prefix='benchday:'),
        Principal('unchain', 'u'*32, 'caller', ('unchain.render_chunk',)),
        Principal('admin', 'm'*32, 'admin', ('attune.produce_item',)),
    ])
    data = b'input fixture'
    digest = hashlib.sha256(data).hexdigest()
    for principal in ('attune-hub', 'benchday-hub', 'unchain'):
        server.blobs.put(principal, digest, len(data), BytesIO(data))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, body=None, token='a'*32):
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/workloads/{path}',
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    yield call, digest, store, server
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def submission(key, digest, **extra):
    return dict(version=1, key=key, handler='attune.produce_item',
                input_digest=digest, need={'cpu': 1}, **extra)


def test_labels_owner_inside_prefix_accepted_and_returned(api):
    call, digest, _, _ = api
    status, job = call('jobs', submission('one', digest, labels={'owner': 'attune:acct_a'}))
    assert status == 200
    assert job['labels'] == {'owner': 'attune:acct_a'}
    assert job['spec']['labels'] == {'owner': 'attune:acct_a'}
    assert call('jobs/'+job['id'])[1]['labels'] == {'owner': 'attune:acct_a'}
    listing = call('jobs')[1]
    assert listing['jobs'][0]['labels'] == {'owner': 'attune:acct_a'}


def test_labels_owner_outside_prefix_refused_403(api):
    call, digest, _, _ = api
    status, body = call('jobs', submission('x', digest, labels={'owner': 'benchday:acct_b'}))
    assert status == 403
    assert 'attune-hub' in body['error'] and 'benchday:acct_b' in body['error']


def test_prefixless_principal_cannot_set_labels_owner(api):
    call, digest, _, _ = api
    spec = dict(version=1, key='one', handler='unchain.render_chunk',
                input_digest=digest, need={'cpu': 1}, labels={'owner': 'unchain:acct_1'})
    status, body = call('jobs', spec, token='u'*32)
    assert status == 403
    assert 'delegate_prefix' in body['error']


def test_labels_are_persisted_on_the_job_row(api):
    call, digest, store, _ = api
    _, job = call('jobs', submission('persisted', digest,
                                     labels={'owner': 'attune:acct_a', 'run_id': 'r7'}))
    reopened = WorkloadStore(store.path, handlers={'attune.produce_item'})
    row = reopened.connect().execute("SELECT labels FROM jobs WHERE id=?", (job['id'],)).fetchone()
    assert json.loads(row[0]) == {'owner': 'attune:acct_a', 'run_id': 'r7'}


def test_plain_labels_and_count_limit(api):
    call, digest, _, _ = api
    status, job = call('jobs', submission('plain', digest, labels={'run_id': 'r7'}))
    assert status == 200 and job['labels'] == {'run_id': 'r7'}
    too_many = {f'k{i}': 'v' for i in range(17)}
    assert call('jobs', submission('toomany', digest, labels=too_many))[0] == 400
    assert call('jobs', submission('edge', digest,
                                   labels={f'k{i}': 'v' for i in range(16)}))[0] == 200


def test_submission_without_labels_keeps_legacy_shape(api):
    call, digest, _, _ = api
    _, job = call('jobs', submission('legacy', digest))
    assert job['labels'] == {}
    assert 'labels' not in job['spec'], 'legacy idempotency bytes stay unchanged'
