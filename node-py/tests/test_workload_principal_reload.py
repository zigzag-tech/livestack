"""Plug-and-play principals: a REAL authority re-reads its config on reload.

Every test here fails on the code before `replace_principals`/SIGHUP existed.
"""
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from io import BytesIO
from threading import Thread

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.service import reload_principals
from livestack_node.workloads.store import WorkloadStore

A, B, W, W2, NEW = 'a'*32, 'b'*32, 'w'*32, 'x'*32, 'n'*32
REPORT = dict(boot='b1', report=dict(capacity={'cpu': 2}, available={'cpu': 2}, labels={},
                                     handlers=['test.v1'], ready=True))


def caller(id, token, **kw):
    return dict(id=id, token=token, role='caller', handlers=['test.v1'], **kw)


def worker(id, token, host='h1'):
    return dict(id=id, token=token, role='worker', worker=id, host=host)


class Authority:
    def __init__(self, tmp_path, principals):
        self.config = tmp_path/'authority.json'
        self.write(principals)
        self.now = [1000.0]
        self.store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'}, clock=lambda: self.now[0])
        self.server = WorkloadServer(('127.0.0.1', 0), self.store,
                                     [Principal(**p) for p in principals])
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        data = b'input'
        self.digest = hashlib.sha256(data).hexdigest()
        self.server.blobs.put('alice', self.digest, len(data), BytesIO(data))

    def write(self, principals):
        self.config.write_text(json.dumps(dict(principals=principals)))

    def reload(self):
        return reload_principals(self.server, self.config, attempts=1, pause=0)

    def client(self, token):
        return WorkloadClient(self.url, token, timeout=5)

    def status(self, token):
        try:
            self.client(token).request('jobs')
            return 200
        except WorkloadError as error:
            return error.status

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


@pytest.fixture
def authority(tmp_path):
    made = Authority(tmp_path, [caller('alice', A), worker('w1', W)])
    yield made
    made.close()


def test_added_worker_registers_and_claims_without_restart(authority):
    assert authority.status(NEW) == 401
    authority.write([caller('alice', A), worker('w1', W), worker('w2', NEW, 'h2')])
    assert authority.reload()
    new = authority.client(NEW)
    new.request('worker/report', REPORT)
    job = authority.client(A).submit(dict(version=1, key='k', handler='test.v1',
                                          input_digest=authority.digest, need={'cpu': 1}))
    assert new.request('worker/claim', {'boot': 'b1'})['assignment']  # a real assignment
    assert authority.store.get('alice', job['id'])['state'] == 'running'


def test_removed_principal_is_refused_and_its_attempt_ends_by_lease(authority):
    w = authority.client(W)
    w.request('worker/report', REPORT)
    job = authority.client(A).submit(dict(version=1, key='k', handler='test.v1',
                                          input_digest=authority.digest, need={'cpu': 1}))
    attempt = w.request('worker/claim', {'boot': 'b1'})['assignment']
    authority.write([caller('alice', A)])
    assert authority.reload()
    with pytest.raises(WorkloadError) as error:
        w.request('worker/heartbeat', dict(boot='b1', attempt_id=attempt['attempt_id'], fence=attempt['fence']))
    assert error.value.status == 401
    # The authority did not kill the attempt; the existing lease does.
    assert authority.store.get('alice', job['id'])['state'] == 'running'
    authority.now[0] += 10_000
    authority.store.sweep()
    assert authority.store.get('alice', job['id'])['state'] != 'running'


def test_removed_caller_is_refused_and_its_job_still_ends(authority):
    job = authority.client(A).submit(dict(version=1, key='k', handler='test.v1',
                                          input_digest=authority.digest, need={'cpu': 1},
                                          deadline=authority.now[0]+60))
    authority.write([caller('bob', B), worker('w1', W)])
    assert authority.reload()
    assert authority.status(A) == 401
    authority.now[0] += 10_000  # nobody ever claims it: the deadline ends it
    authority.store.sweep()
    assert authority.store.get('alice', job['id'])['state'] == 'expired'


def test_token_rotation_swaps_credentials(authority):
    assert authority.status(A) == 200
    authority.write([caller('alice', B), worker('w1', W)])
    assert authority.reload()
    assert authority.status(A) == 401
    assert authority.status(B) == 200


def test_operator_drain_preserves_live_handoff_and_resumes_same_boot(authority):
    client = authority.client(W)
    client.request('worker/report', REPORT)
    spec = dict(version=1, key='first', handler='test.v1',
                input_digest=authority.digest, need={'cpu': 1})
    first = authority.client(A).submit(spec)
    attempt = client.request('worker/claim', {'boot': 'b1'})['assignment']
    assert attempt['job_id'] == first['id']
    second = authority.client(A).submit(dict(spec, key='second'))
    paused = dict(worker('w1', W), claim_enabled=False)
    authority.write([caller('alice', A), paused])
    assert authority.reload()
    assert client.request('worker/claim', {'boot': 'b1'}) == {
        'assignment': None, 'reason': 'worker_draining'}
    identity = dict(boot='b1', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    assert client.request('worker/heartbeat', identity)['lease_remaining'] > 0
    source = authority.config.parent/'handoff'
    source.write_bytes(b'completed during drain')
    from livestack_node.workloads.transfer import InputTransfer
    artifact = InputTransfer(client).put(source, assignment=attempt)
    complete = client.request('worker/complete', dict(identity,
        input_digest=authority.digest, outcome='succeeded',
        result={'artifacts': [dict(name='handoff', **artifact)]}))
    assert complete['state'] == 'succeeded'
    assert authority.client(A).get(second['id'])['state'] == 'queued'
    client.request('worker/report', REPORT)
    assert client.request('worker/claim', {'boot': 'b1'})['reason'] == 'worker_draining'
    authority.write([caller('alice', A), worker('w1', W)])
    assert authority.reload()
    resumed = client.request('worker/claim', {'boot': 'b1'})['assignment']
    assert resumed['job_id'] == second['id'] and resumed['boot'] == 'b1'


def test_other_worker_claim_cannot_place_work_on_a_drained_worker(authority):
    one, two = authority.client(W), authority.client(W2)
    authority.write([caller('alice', A), worker('w1', W), worker('w2', W2, 'h2')])
    assert authority.reload()
    one.request('worker/report', REPORT)
    two.request('worker/report', REPORT)
    authority.write([caller('alice', A), dict(worker('w1', W), claim_enabled=False),
                     worker('w2', W2, 'h2')])
    assert authority.reload()
    job = authority.client(A).submit(dict(version=1, key='cross-worker', handler='test.v1',
        input_digest=authority.digest, need={'cpu': 1}))
    assignment = two.request('worker/claim', {'boot': 'b1'})['assignment']
    assert assignment is not None and assignment['worker'] == 'w2'
    assert assignment['job_id'] == job['id']
    with authority.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM attempts WHERE worker='w1'").fetchone()[0] == 0


def test_drained_worker_does_not_hold_an_infrastructure_retry(authority):
    one, two = authority.client(W), authority.client(W2)
    one.request('worker/report', REPORT)
    job = authority.client(A).submit(dict(version=1, key='retry-drain', handler='test.v1',
        input_digest=authority.digest, need={'cpu': 1}))
    first = one.request('worker/claim', {'boot': 'b1'})['assignment']
    authority.write([caller('alice', A), worker('w1', W),
                     dict(worker('w2', W2, 'h2'), claim_enabled=False)])
    assert authority.reload()
    two.request('worker/report', REPORT)
    one.request('worker/complete', dict(boot='b1', attempt_id=first['attempt_id'],
        fence=first['fence'], input_digest=authority.digest, outcome='infrastructure',
        result={'error': 'disposable infrastructure failure'}))
    retry = one.request('worker/claim', {'boot': 'b1'})['assignment']
    assert retry is not None and retry['job_id'] == job['id'] and retry['worker'] == 'w1'


@pytest.mark.parametrize('label, content', [
    ('zero principals', json.dumps(dict(principals=[]))),
    ('duplicate tokens', json.dumps(dict(principals=[caller('alice', A), caller('bob', A)]))),
    ('malformed json', '{"principals": ['),
    ('torn write', ''),
    ('unknown role', json.dumps(dict(principals=[dict(id='x', token=A, role='root')]))),
    ('worker without host', json.dumps(dict(principals=[dict(id='w', token=W, role='worker', worker='w')]))),
    ('caller without handlers', json.dumps(dict(principals=[dict(id='x', token=B, role='caller')]))),
    ('unknown field', json.dumps(dict(principals=[caller('alice', A, bogus=1)]))),
    ('nonboolean drain', json.dumps(dict(principals=[caller('alice', A), dict(worker('w1', W), claim_enabled=0)]))),
    ('caller drain', json.dumps(dict(principals=[caller('alice', A, claim_enabled=False), worker('w1', W)]))),
    ('no principals key', json.dumps({})),
    ('role changed', json.dumps(dict(principals=[worker('alice', A)]))),
    ('worker rehosted', json.dumps(dict(principals=[caller('alice', A), worker('w1', W, 'other')]))),
])
def test_invalid_config_keeps_previous_set(authority, caplog, label, content):
    authority.config.write_text(content)
    with caplog.at_level(logging.ERROR):
        assert not authority.reload()
    assert 'principal_reload_refused' in caplog.text
    assert A not in caplog.text and W not in caplog.text
    assert authority.status(A) == 200
    authority.client(W).request('worker/report', REPORT)
    assert authority.store.principals.keys() == {'alice', 'w1'}


def test_missing_file_keeps_previous_set(authority, caplog):
    authority.config.unlink()
    with caplog.at_level(logging.ERROR):
        assert not authority.reload()
    assert 'principal_reload_refused' in caplog.text
    assert authority.status(A) == 200


def test_torn_read_is_retried_until_the_write_completes(authority):
    authority.config.write_text('{"princ')
    good = json.dumps(dict(principals=[caller('alice', A), caller('bob', B), worker('w1', W)]))
    Thread(target=lambda: (time.sleep(.15), authority.config.write_text(good))).start()
    assert reload_principals(authority.server, authority.config, attempts=10, pause=.1)
    assert authority.status(B) == 200


def test_unchanged_principal_keeps_state_and_changed_cap_applies(tmp_path):
    made = Authority(tmp_path, [caller('alice', A, max_running=1, on_cap='refuse'), worker('w1', W)])
    try:
        w = made.client(W)
        w.request('worker/report', REPORT)
        made.client(A).submit(dict(version=1, key='k', handler='test.v1',
                                   input_digest=made.digest, need={'cpu': 1}))
        w.request('worker/claim', {'boot': 'b1'})
        before = made.client(A).request('jobs')['principal']
        assert before == {'max_running': 1, 'running': 1}
        made.write([caller('alice', A, max_running=1, on_cap='refuse'), worker('w1', W), caller('bob', B)])
        assert made.reload()
        assert made.client(A).request('jobs')['principal'] == before  # running count survived
        with pytest.raises(WorkloadError) as error:  # the cap still binds
            made.client(A).submit(dict(version=1, key='k2', handler='test.v1',
                                       input_digest=made.digest, need={'cpu': 1}))
        assert error.value.status == 429
        made.write([caller('alice', A, max_running=5), worker('w1', W)])
        assert made.reload()
        assert made.client(A).request('jobs')['principal'] == {'max_running': 5, 'running': 1}
    finally:
        made.close()


def test_concurrent_requests_never_see_a_half_applied_set(authority):
    """alice and w1 are in every set; a reload swapping the others must never
    make either of them unauthenticated, and each request sees old or new."""
    sets = [[caller('alice', A), worker('w1', W), caller('bob', B)],
            [caller('alice', A), worker('w1', W), caller('carol', NEW)]]
    stop, bad = [False], []

    def hammer():
        while not stop[0]:
            for token in (A, W):
                status = authority.status(token) if token == A else \
                    _status(authority.client(token), 'worker/report')
                if status in (401, 503):
                    bad.append((token[:1], status))

    def _status(client, route):
        try:
            client.request(route, REPORT)
            return 200
        except WorkloadError as error:
            return error.status

    threads = [Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for i in range(40):
        authority.write(sets[i % 2])
        assert authority.reload()
    stop[0] = True
    for t in threads:
        t.join(timeout=10)
    assert not bad, bad[:5]
    assert len(authority.server.principals) == 3


def test_sighup_reloads_the_real_service_process(tmp_path):
    config, state = tmp_path/'authority.json', tmp_path/'state'
    def write(principals):
        tmp = tmp_path/'authority.json.tmp'
        tmp.write_text(json.dumps(dict(state_dir=str(state), port=0, handlers=['test.v1'],
                                       principals=principals)))
        os.replace(tmp, config)  # the editor pattern: write temp, rename
    write([caller('alice', A)])
    process = subprocess.Popen([sys.executable, '-m', 'livestack_node.workloads.service',
                                '--config', str(config)])
    try:
        log, deadline, match = state/'authority.log', time.monotonic()+15, None
        while not match:
            if log.exists():
                match = re.search(r"started on \('127.0.0.1', (\d+)\)", log.read_text())
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.05)
        url = f'http://127.0.0.1:{match[1]}'
        def status(token):
            try:
                WorkloadClient(url, token).request('jobs')
                return 200
            except WorkloadError as error:
                return error.status
        assert (status(A), status(B)) == (200, 401)
        write([caller('alice', A), caller('bob', B)])
        process.send_signal(signal.SIGHUP)
        deadline = time.monotonic()+10
        while status(B) != 200:
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.05)
        config.write_text('{ torn')
        process.send_signal(signal.SIGHUP)
        deadline = time.monotonic()+10
        while 'principal_reload_refused' not in log.read_text():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.05)
        assert (status(A), status(B)) == (200, 200)  # the bad file changed nothing
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=10)
