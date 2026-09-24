"""J.2: attempt environment carries HARMONY_OWNER/HARMONY_FLEET_URL/
HARMONY_FLEET_TOKEN; lease_helper admits under the label owner; attempt
cleanup releases leases.json leftovers even after a killed handler."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time

import pytest

import livestack_node
from livestack_node.workloads import lease_helper
from livestack_node.workloads.archive import capture
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer
from livestack_node.workloads.worker import WorkloadWorker


class FakeFleetBroker:
    """Records /fleet/admit calls and lease releases; a released lease is no
    longer live. Stands in for the Harmony fleet broker."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, value):
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(length) or b'{}')
                if self.path == '/fleet/admit':
                    lease_id = f"lease-{len(broker.admits)}"
                    broker.admits.append({'body': body,
                                          'authorization': self.headers.get('Authorization')})
                    broker.live.add(lease_id)
                    self._send({'granted': True, 'lease_id': lease_id,
                                'target': {'node': 'fake-node'}, 'reason': 'fake'})
                    return
                parts = self.path.strip('/').split('/')
                if len(parts) == 3 and parts[0] == 'lease' and parts[2] == 'heartbeat':
                    self._send({'ok': parts[1] in broker.live})
                    return
                if len(parts) == 3 and parts[0] == 'lease' and parts[2] == 'release':
                    broker.releases.append(parts[1])
                    broker.release_bodies.append(body)
                    broker.live.discard(parts[1])
                    self._send({'ok': True})
                    return
                self._send({'error': 'unknown route'})

        self.admits, self.releases, self.live = [], [], set()
        self.release_bodies = []
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


@pytest.fixture
def fleet_broker():
    broker = FakeFleetBroker()
    yield broker
    broker.close()


def worker_config(tmp_path, authority, token='w'*32, **extra):
    return dict(authority=authority, token=token, worker='integration',
        state_dir=str(tmp_path/'state'), workspace=str(tmp_path/'workspace'),
        require_dedicated_filesystem=False, lease_interval=.2,
        capacity={'cpu':1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},
        memory_reserve_bytes=0, disk_reserve_bytes=0,
        environment={'PATH':'/usr/bin:/bin'},
        handlers={'native.v1': dict(argv=['/bin/true'])}, **extra)


def test_attempt_env_carries_owner_and_fleet_credentials(tmp_path, fleet_broker):
    config = worker_config(tmp_path, 'http://127.0.0.1:1',
                           fleet_url=fleet_broker.url, fleet_token='f'*32)
    worker = WorkloadWorker(config)
    root, output, objects = (tmp_path/p for p in ('root', 'output', 'objects'))
    for path in (root, output, objects):
        path.mkdir()
    assignment = {'owner': 'attune-hub', 'attempt_id': 'a'*32,
                  'spec': {'labels': {'owner': 'attune:acct_a'}, 'payload': {}}}
    env = worker._attempt_env(assignment, root, output, objects, 'attempt1')
    assert env['HARMONY_OWNER'] == 'attune:acct_a'
    assert env['HARMONY_FLEET_URL'] == fleet_broker.url
    assert env['HARMONY_FLEET_TOKEN'] == 'f'*32
    # Without a label owner the principal itself owns the attempt.
    unlabelled = dict(assignment, spec={'payload': {}})
    assert worker._attempt_env(unlabelled, root, output, objects, 'attempt1')['HARMONY_OWNER'] == 'attune-hub'
    # No fleet configured: no fleet env leaks into the handler.
    plain = WorkloadWorker(worker_config(tmp_path/'plain', 'http://127.0.0.1:1'))
    env = plain._attempt_env(unlabelled, root, output, objects, 'attempt1')
    assert 'HARMONY_FLEET_URL' not in env and 'HARMONY_FLEET_TOKEN' not in env


def test_cleanup_releases_leases_left_by_a_crashed_handler(tmp_path, fleet_broker, monkeypatch):
    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (0, 0, 0))
    worker = WorkloadWorker(worker_config(tmp_path, 'http://127.0.0.1:1',
                                          fleet_url=fleet_broker.url, fleet_token='f'*32))
    output = tmp_path/'output'
    output.mkdir()
    lease = lease_helper.admit(fleet_broker.url, 'f'*32, 'attune:acct_a', 'polytts',
                               output_dir=output)
    assert lease['lease_id'] in fleet_broker.live
    worker._release_fleet_leases(output)  # the worker's post-crash cleanup path
    assert fleet_broker.releases == [lease['lease_id']]
    assert not fleet_broker.live, 'a crashed handler leaves no live lease'
    assert not (output/'leases.json').exists()
    # A crash-recovery cleanup never watched the workload: it reports nothing.
    assert fleet_broker.release_bodies == [{}]


def test_release_reports_the_workloads_status_and_wall_time(tmp_path, fleet_broker):
    """scheduler-policy-routine 3.4: the release body carries how the job went,
    which the fleet broker joins to the decision that placed it."""
    worker = WorkloadWorker(worker_config(tmp_path, 'http://127.0.0.1:1',
                                          fleet_url=fleet_broker.url, fleet_token='f'*32))
    output = tmp_path/'output'
    output.mkdir()
    lease_helper.admit(fleet_broker.url, 'f'*32, 'attune:acct_a', 'polytts', output_dir=output)
    worker._release_fleet_leases(output, status='ok', wall_s=41.23456)
    assert fleet_broker.release_bodies == [{'status': 'ok', 'wall_s': 41.235}]
    lease_helper.release(fleet_broker.url, 'lease-9', status='failed', wall_s=2)
    assert fleet_broker.release_bodies[-1] == {'status': 'failed', 'wall_s': 2.0}


def test_a_failed_release_does_not_fail_the_workload(tmp_path, fleet_broker):
    worker = WorkloadWorker(worker_config(tmp_path, 'http://127.0.0.1:1',
                                          fleet_url='http://127.0.0.1:1', fleet_token='f'*32))
    output = tmp_path/'output'
    output.mkdir()
    (output/'leases.json').write_text(json.dumps(['lease-x']))
    worker._release_fleet_leases(output, status='ok', wall_s=1.0)   # must not raise


@pytest.fixture
def authority(tmp_path, monkeypatch, fleet_broker):
    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (0, 0, 0))
    if subprocess.run(['systemctl', '--user', 'show'], capture_output=True).returncode:
        pytest.skip('requires Linux systemd user manager and cgroup v2')
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'native.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('attune-hub', 'a'*32, 'caller', ('native.v1',), delegate_prefix='attune:'),
        Principal('worker', 'w'*32, 'worker', worker='integration', host='test-host')])
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = tmp_path/'lease-handler.py'
    package_parent = str(Path(livestack_node.__file__).resolve().parent.parent)
    script.write_text(f'''import os, sys, time
sys.path.insert(0, {package_parent!r})
from livestack_node.workloads import lease_helper
lease = lease_helper.admit(os.environ['HARMONY_FLEET_URL'],
                           os.environ['HARMONY_FLEET_TOKEN'],
                           os.environ['HARMONY_OWNER'], 'polytts')
print('admitted', lease['lease_id'], flush=True)
while True:
    time.sleep(1)
''')
    url = f'http://127.0.0.1:{server.server_port}'
    config = worker_config(tmp_path, url, fleet_url=fleet_broker.url, fleet_token='f'*32)
    config['handlers'] = {'native.v1': dict(argv=[sys.executable, str(script)], outputs=[])}
    caller = WorkloadClient(url, 'a'*32)
    source = tmp_path/'source'
    source.mkdir()
    (source/'input').write_text('captured bytes')
    capture(source, ['input'], tmp_path/'source.tar')
    digest = InputTransfer(caller).put(tmp_path/'source.tar')['digest']
    yield caller, config, digest, fleet_broker
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_grant_names_label_owner_and_killed_handler_leaves_no_live_lease(authority):
    caller, config, digest, broker = authority
    job = caller.submit(dict(version=1, key='lease-job', handler='native.v1',
                             input_digest=digest, labels={'owner': 'attune:acct_a'},
                             need={'cpu':.1,'memory_bytes':128*1024**2,'disk_bytes':64*1024**2},
                             payload={}))
    worker = WorkloadWorker(config)
    stepping = Thread(target=worker.step)
    stepping.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not broker.admits:
            time.sleep(.05)
        assert len(broker.admits) == 1, 'the handler admitted through the fake fleet broker'
        admit = broker.admits[0]
        assert admit['body']['owner'] == 'attune:acct_a', \
            'the Grant names the label owner, not the worker'
        assert admit['body']['kind'] == 'polytts'
        assert admit['authorization'] == 'Bearer ' + 'f'*32
        # Kill the handler mid-run: the attempt cleanup must release the lease.
        attempt = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            record = worker.journal.read()
            if record and record.get('phase') == 'running':
                attempt = record['assignment']['attempt_id']
                break
            time.sleep(.05)
        assert attempt, 'the handler reached its running phase'
        worker.executor.stop(attempt)
        stepping.join(timeout=60)
        assert not stepping.is_alive(), 'the worker finished the killed attempt'
    finally:
        worker.close()
    assert broker.releases == ['lease-0'], broker.releases
    assert not broker.live, 'a killed handler leaves no live lease'
    # The worker watched it run, so the release says how it went.
    [body] = broker.release_bodies
    assert body['status'] == 'failed' and body['wall_s'] > 0
    result = caller.get(job['id'])
    assert result['state'] in ('queued', 'failed'), \
        'a handler killed without an exit receipt is an infrastructure retry'
