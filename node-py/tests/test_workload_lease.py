"""Real authority cancellation reaches a real cgroup without caller ownership."""
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time

import pytest

from livestack_node.workloads.archive import capture
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.lease import LeaseKeeper
from livestack_node.workloads.model import Limits
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.supervision import SystemdExecutor
from livestack_node.workloads.transfer import InputTransfer


class LeaseClient:
    timeout = .1

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = 0

    def request(self, route, body):
        self.calls += 1
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return {'lease_remaining': reply}


def test_transient_renewal_retries_within_the_existing_lease(tmp_path):
    client = LeaseClient([.6, TimeoutError(), .6, .6])
    lease = LeaseKeeper(client, {'boot':'b', 'attempt_id':'a', 'fence':1},
                        tmp_path/'lease', interval=.02).start()
    try:
        deadline = time.monotonic()+.4
        while client.calls < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        assert client.calls >= 3
        assert not lease.lost.is_set()
        assert float((tmp_path/'lease').read_text()) > time.monotonic()
    finally:
        lease.close()


def test_authority_refusal_revokes_without_waiting_for_expiry(tmp_path):
    client = LeaseClient([.6, WorkloadError('cancelled', 409)])
    lease = LeaseKeeper(client, {'boot':'b', 'attempt_id':'a', 'fence':1},
                        tmp_path/'lease', interval=.02).start()
    try:
        assert lease.lost.wait(.3)
        assert (tmp_path/'lease').read_text() == '0'
    finally:
        lease.close()


def test_unreachable_authority_cannot_extend_the_existing_lease(tmp_path):
    client = LeaseClient([.12, *[TimeoutError() for _ in range(20)]])
    lease = LeaseKeeper(client, {'boot':'b', 'attempt_id':'a', 'fence':1},
                        tmp_path/'lease', interval=.01).start()
    try:
        assert lease.lost.wait(.5)
        assert (tmp_path/'lease').read_text() == '0'
    finally:
        lease.close()


@pytest.mark.parametrize('predicate,error', [
    (lambda: False, 'WorkNotAlive'),
    (lambda: (_ for _ in ()).throw(OSError('unknown')), 'OSError'),
])
def test_unproved_execution_liveness_stops_renewal(tmp_path, predicate, error):
    client = LeaseClient([.6] * 20)
    lease = LeaseKeeper(client, {'boot':'b', 'attempt_id':'a', 'fence':1},
                        tmp_path/'lease', interval=.02).start()
    try:
        lease.require_liveness(predicate)
        assert lease.lost.wait(.3)
        calls = client.calls
        time.sleep(.05)
        assert client.calls == calls
        assert lease.error == error
        assert (tmp_path/'lease').read_text() == '0'
    finally:
        lease.close()


@pytest.mark.parametrize('state,expected', [
    ({'LoadState':'loaded', 'ActiveState':'active'}, True),
    ({'LoadState':'loaded', 'ActiveState':'activating'}, True),
    ({'LoadState':'loaded', 'ActiveState':'inactive'}, False),
    ({'LoadState':'not-found', 'ActiveState':'inactive'}, False),
    ({}, False),
])
def test_executor_liveness_fails_closed(monkeypatch, state, expected):
    executor = SystemdExecutor('lease-liveness')
    monkeypatch.setattr(executor, 'inspect', lambda _attempt: state)
    assert executor.alive('a'*32) is expected


def test_worker_renews_after_caller_disconnect_then_cancellation_stops_cgroup(tmp_path):
    if subprocess.run(['systemctl', '--user', 'show'], capture_output=True).returncode:
        pytest.skip('requires systemd user manager and cgroup v2')
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'test.v1'}, limits=Limits(lease_seconds=2))
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('owner', 'a'*32, 'caller', ('test.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='test-worker', host='test-host')])
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    lease, a = None, None
    executor = SystemdExecutor('lease-integration')
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        caller, worker = WorkloadClient(url, 'a'*32), WorkloadClient(url, 'w'*32)
        source = tmp_path/'source'
        source.mkdir()
        (source/'input').write_text('immutable')
        capture(source, ['input'], tmp_path/'bundle.tar')
        digest = InputTransfer(caller).put(tmp_path/'bundle.tar')['digest']
        job = caller.submit(dict(version=1, key='one', handler='test.v1', input_digest=digest, need={'cpu':1}))
        del caller  # No submitter session participates in subsequent renewal.
        worker.request('worker/report', dict(boot='b1', report=dict(
            capacity={'cpu':2}, available={'cpu':2}, labels={}, handlers=['test.v1'], ready=True)))
        a = worker.request('worker/claim', {'boot':'b1'})['assignment']
        lease = LeaseKeeper(worker, a, tmp_path/'lease', interval=.2).start()
        executor.start(a['attempt_id'], [sys.executable, '-c', 'import time; time.sleep(120)'],
                       source, tmp_path/'out', env=dict(os.environ), cpu=1, memory_bytes=128*1024**2,
                       lease_file=tmp_path/'lease')
        time.sleep(2.5)  # Exceeds original lease: this requires real renewals.
        assert not lease.lost.is_set()
        assert store.get('owner', job['id'])['state'] == 'running'
        group = executor.inspect(a['attempt_id'])['ControlGroup']
        assert group
        WorkloadClient(url, 'a'*32).request('jobs/'+job['id']+'/cancel', {})
        assert lease.lost.wait(3)
        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        deadline = time.monotonic()+8
        while cgroup.exists() and 'populated 1' in (cgroup/'cgroup.events').read_text():
            assert time.monotonic() < deadline
            time.sleep(.1)
        assert store.get('owner', job['id'])['state'] == 'cancelled'
    finally:
        if lease:
            lease.close()
        if a:
            executor.stop(a['attempt_id'])
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_removed_execution_stops_renewal_and_authority_requeues(tmp_path):
    """A live worker process cannot keep a vanished supervised unit leased."""
    if subprocess.run(['systemctl', '--user', 'show'], capture_output=True).returncode:
        pytest.skip('requires systemd user manager and cgroup v2')
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'test.v1'}, limits=Limits(lease_seconds=2))
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('owner', 'a'*32, 'caller', ('test.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='test-worker', host='test-host')])
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    lease, assignment = None, None
    executor = SystemdExecutor('lease-disappeared-integration')
    worker_pid = os.getpid()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        caller, worker = WorkloadClient(url, 'a'*32), WorkloadClient(url, 'w'*32)
        source = tmp_path/'source'
        source.mkdir()
        (source/'input').write_text('immutable')
        capture(source, ['input'], tmp_path/'bundle.tar')
        digest = InputTransfer(caller).put(tmp_path/'bundle.tar')['digest']
        job = caller.submit(dict(version=1, key='vanished', handler='test.v1',
                                 input_digest=digest, need={'cpu':1}))
        worker.request('worker/report', dict(boot='b1', report=dict(
            capacity={'cpu':2}, available={'cpu':2}, labels={}, handlers=['test.v1'], ready=True)))
        assignment = worker.request('worker/claim', {'boot':'b1'})['assignment']
        output = tmp_path/'out'
        lease = LeaseKeeper(worker, assignment, tmp_path/'lease', interval=.1).start()
        executor.start(assignment['attempt_id'], [sys.executable, '-c', 'import time; time.sleep(120)'],
                       source, output, env=dict(os.environ), cpu=1, memory_bytes=128*1024**2,
                       lease_file=tmp_path/'lease')
        lease.require_liveness(lambda: (executor.exit_result(output) is not None or
                                        executor.alive(assignment['attempt_id'])))

        initial_expiry = assignment['expires']
        deadline = time.monotonic()+3
        while store.get('owner', job['id'])['attempts'][0]['expires'] <= initial_expiry:
            assert time.monotonic() < deadline
            time.sleep(.05)

        # Remove only the owned execution. The process hosting the worker and
        # LeaseKeeper remains alive, reproducing the production ghost shape.
        executor.command('systemctl', '--user', 'stop', executor.unit(assignment['attempt_id']))
        executor.command('systemctl', '--user', 'reset-failed', executor.unit(assignment['attempt_id']), check=False)
        assert os.getpid() == worker_pid
        assert lease.lost.wait(1)
        stopped_expiry = store.get('owner', job['id'])['attempts'][0]['expires']
        time.sleep(.35)
        assert store.get('owner', job['id'])['attempts'][0]['expires'] == stopped_expiry

        time.sleep(max(0, stopped_expiry-time.time()+.1))
        store.sweep()
        recovered = store.get('owner', job['id'])
        assert recovered['state'] == 'queued'
        assert recovered['reason'] == 'execution lease expired'
        assert recovered['attempts'][0]['state'] == 'cleanup'
    finally:
        if lease:
            lease.close()
        if assignment:
            executor.stop(assignment['attempt_id'])
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
