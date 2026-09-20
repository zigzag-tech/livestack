"""J.3/J.4: per-principal max_running cap in placement; 429 on refuse; the
umbrella scenario — 40 queued attune jobs at cap 2 never starve a benchday
thumbnail submitted behind them."""
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
        'attune.produce_item', 'benchday.thumbnail', 'sorbonne.asr'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('attune-hub', 'a'*32, 'caller', ('attune.produce_item',),
                  delegate_prefix='attune:', max_running=2),
        Principal('benchday-hub', 'b'*32, 'caller', ('benchday.thumbnail',),
                  delegate_prefix='benchday:', max_running=4),
        Principal('sorbonne', 's'*32, 'caller', ('sorbonne.asr',),
                  max_running=1, on_cap='refuse'),
        Principal('w1', 'w1'*32, 'worker', worker='w1', host='host1'),
        Principal('w2', 'w2'*32, 'worker', worker='w2', host='host2'),
        Principal('w3', 'w3'*32, 'worker', worker='w3', host='host3'),
    ])
    data = b'input fixture'
    digest = hashlib.sha256(data).hexdigest()
    for principal in ('attune-hub', 'benchday-hub', 'sorbonne'):
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

    def worker(name, host, boot, handlers):
        report = dict(boot=boot, report=dict(
            capacity={'cpu': 4, 'memory_bytes': 8*1024**3, 'disk_bytes': 64*1024**3},
            available={'cpu': 4, 'memory_bytes': 8*1024**3, 'disk_bytes': 64*1024**3},
            labels={}, handlers=handlers, ready=True))
        status, body = call('worker/report', report, token=name*32)
        assert status == 200 and body['worker'] == name
    yield call, digest, worker
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def submit(call, token, key, handler, digest, **extra):
    return call('jobs', dict(version=1, key=key, handler=handler,
                             input_digest=digest, need={'cpu': 1}, **extra), token=token)


def claim(call, name, boot):
    status, body = call('worker/claim', {'boot': boot}, token=name*32)
    assert status == 200
    return body['assignment']


def test_capped_principals_backlog_does_not_starve_other_owners(api):
    call, digest, worker = api
    for index in range(3):
        worker(f'w{index+1}', f'host{index+1}', f'boot{index+1}',
               ['attune.produce_item', 'benchday.thumbnail'])
    # 40 attune jobs, deliberately HIGHER priority than the thumbnail: the cap
    # must skip them without blocking the queue behind them.
    for index in range(40):
        status, _ = submit(call, 'a'*32, f'attune-{index}', 'attune.produce_item',
                           digest, priority=1000)
        assert status == 200
    a1 = claim(call, 'w1', 'boot1')
    a2 = claim(call, 'w2', 'boot2')
    assert a1['owner'] == 'attune-hub' and a2['owner'] == 'attune-hub'
    status, thumb = submit(call, 'b'*32, 'thumb-1', 'benchday.thumbnail', digest)
    assert status == 200
    placed = claim(call, 'w3', 'boot3')
    assert placed['job_id'] == thumb['id'], 'the thumbnail places while attune runs at its cap'
    assert placed['spec']['handler'] == 'benchday.thumbnail'
    # No third attune attempt was created; queued jobs name the cap. (The
    # probe below postdates the last placement pass, so it carries no reason.)
    _, probe = submit(call, 'a'*32, 'probe', 'attune.produce_item', digest)
    assert probe['state'] == 'queued'
    _, earlier = call('jobs/' + a1['job_id'])
    assert earlier['state'] == 'running'
    attune = call('jobs', token='a'*32)[1]
    capped = [j for j in attune['jobs'] if j['state'] == 'queued' and j['id'] != probe['id']]
    assert len(capped) == len(attune['jobs']) - 1, 'every listed job except the probe is queued'
    assert all(j['reason'] == 'principal at max_running (2)' for j in capped)


def test_job_list_reports_principal_cap_and_running_count(api):
    call, digest, worker = api
    worker('w1', 'host1', 'boot1', ['attune.produce_item', 'benchday.thumbnail'])
    for index in range(3):
        submit(call, 'a'*32, f'attune-{index}', 'attune.produce_item', digest)
    claim(call, 'w1', 'boot1')
    body = call('jobs', token='a'*32)[1]
    assert body['principal'] == {'max_running': 2, 'running': 1}
    body = call('jobs', token='b'*32)[1]
    assert body['principal'] == {'max_running': 4, 'running': 0}


def test_refuse_policy_answers_429_naming_the_count(api):
    call, digest, worker = api
    worker('w1', 'host1', 'boot1', ['sorbonne.asr'])
    status, first = submit(call, 's'*32, 'one', 'sorbonne.asr', digest)
    assert status == 200
    claim(call, 'w1', 'boot1')  # the principal is now at its cap of one
    status, body = submit(call, 's'*32, 'two', 'sorbonne.asr', digest)
    assert status == 429
    assert 'principal at max_running (1)' in body['error']
    # The refused submission created no job.
    listing = call('jobs', token='s'*32)[1]
    assert [j['request_key'] for j in listing['jobs']] == ['one']
