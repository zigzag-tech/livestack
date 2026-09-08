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
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.supervision import SystemdExecutor
from livestack_node.workloads.transfer import InputTransfer


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
