"""Task-environment lease expiry with a real rootless container and worker."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread
import time

import pytest

from livestack_node.workloads.archive import capture
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.docker_runtime import runtime_path
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import Limits
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.task_environments import TaskEnvironmentStore
from livestack_node.workloads.transfer import InputTransfer
from livestack_node.workloads.worker import WorkloadWorker


IMAGE = 'docker.m.daocloud.io/library/alpine@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d'


def test_lease_expiry_cleans_rootless_container_before_releasing_task_environment(tmp_path, monkeypatch):
    if sys.platform != 'linux' or subprocess.run(
            ['systemctl', '--user', 'show'], capture_output=True).returncode:
        pytest.skip('requires Linux systemd user manager and cgroup v2')
    if not all(shutil.which(tool) for tool in ('rootlesskit', 'slirp4netns', 'newuidmap', 'dockerd', 'docker')):
        pytest.skip('requires installed rootless Docker prerequisites')
    if not shutil.which('sudo') or subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode:
        pytest.skip('requires passwordless systemd service-manager access for isolated rootless Docker')

    monkeypatch.setattr('livestack_node.workloads.worker.os.getloadavg', lambda: (0, 0, 0))
    lease_seconds = 30
    clock_offset = [0]
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'native.v1'},
        environment_handlers={'native.v1': {'purpose': 'development', 'profile': 'native-test-v1'}},
        limits=Limits(lease_seconds=lease_seconds))
    store_clock = store.clock
    store.clock = lambda: store_clock() + clock_offset[0]
    principals = [
        Principal('owner', 'a'*32, 'caller', ('native.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='taskenv-docker', host='test-host')]
    server = WorkloadServer(('127.0.0.1', 0), store, principals)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    caller = WorkloadClient(url, 'a'*32)
    environment_root = tmp_path/'task-environments'
    environment_root.mkdir()
    script = tmp_path/'rootless-task-handler.py'
    script.write_text(f'''import json,os,subprocess,sys,time
from pathlib import Path
image = {IMAGE!r}
assert not Path('/var/run/docker.sock').exists(), 'host Docker socket leaked into namespace'
cache = Path('build')/'cache.txt'
cache.parent.mkdir(parents=True, exist_ok=True)
cache.write_text('partial build state')
subprocess.run(['docker','run','-d','--name=taskenv-expiry',image,'sleep','120'],check=True)
pid = int(subprocess.check_output(['docker','inspect','--format','{{{{.State.Pid}}}}','taskenv-expiry'],text=True))
group = Path(f'/proc/{{pid}}/cgroup').read_text().strip().split('::',1)[1]
Path(os.environ['HARMONY_OUTPUT'],'container.json').write_text(json.dumps({{'pid':pid,'group':group}}))
time.sleep(120)
''')

    config = dict(authority=url, token='w'*32, worker='taskenv-docker',
        state_dir=str(tmp_path/'worker-state'), workspace=str(tmp_path/'worker-workspace'),
        require_dedicated_filesystem=False, lease_interval=.2,
        handler_runtimes={'python3': sys.executable},
        capacity={'cpu': 1, 'memory_bytes': 512*1024**2, 'disk_bytes': 64*1024**2},
        memory_reserve_bytes=0, disk_reserve_bytes=0, environment={'PATH': '/usr/bin:/bin'},
        handlers={'native.v1': dict(argv=[sys.executable, str(script)], outputs=['container.json'],
                                    backend='rootless-docker')},
        task_environments=dict(root=str(environment_root), host_id='test-host',
            quota_helper='/unused-in-tests', project_id_min=100000, project_id_max=100063,
            max_bytes_per_replica=32*1024**3, max_bytes_per_owner=128*1024**3,
            max_total_bytes=256*1024**3, reserve_bytes=0, profiles={'native-test-v1': {
                'handlers': ['native.v1'], 'purpose': 'development', 'cache_contract': 'native-test-v1',
                'probe_argv': [sys.executable, '-c', 'print("native-test-toolchain-v1")'],
                'cache_components': [{'name': 'incremental-build', 'path': 'source/build',
                    'inputs': [], 'contract': 'incremental-build-v1'}]}}))

    def quota_usage(project_ids):
        rows = []
        for marker_path in environment_root.glob('*/environment.json'):
            marker = json.loads(marker_path.read_text())
            if marker['project_id'] in project_ids:
                rows.append({'project_id': marker['project_id'],
                    'used_bytes': sum(item.lstat().st_blocks*512 for item in marker_path.parent.rglob('*')
                                      if not item.is_dir()),
                    'hard_bytes': ((marker['quota_bytes']+1023)//1024)*1024})
        return rows

    original_init = TaskEnvironmentStore.__init__

    def test_store_init(self, environment_config, **kwargs):
        return original_init(self, environment_config, **kwargs,
            quota_ensure=lambda _handle, _project, quota: {'quota_bytes': quota},
            quota_probe=lambda _root: True, quota_usage=quota_usage,
            require_separate_filesystem=False, filesystem_bytes=8*1024**3)

    monkeypatch.setattr(TaskEnvironmentStore, '__init__', test_store_init)
    worker = WorkloadWorker(config)
    worker_thread = None
    worker_errors = []
    attempt = None
    container_pid = None
    job = None
    try:
        source = tmp_path/'source'
        source.mkdir()
        (source/'input').write_text('captured task input')
        capture(source, ['input'], tmp_path/'input.tar')
        digest = InputTransfer(caller).put(tmp_path/'input.tar')['digest']
        job = caller.submit(dict(version=3, key='lease-expiry-rootless', handler='native.v1',
            input_digest=digest,
            need={'cpu': .5, 'memory_bytes': 512*1024**2, 'disk_bytes': 64*1024**2},
            environment={'key': 'lease-expiry-task', 'reuse': 'prefer'}, payload={}))
        handle = job['environment_handle']

        def execute():
            try:
                worker.step()
            except Exception as error:
                worker_errors.append(error)

        worker_thread = Thread(target=execute)
        worker_thread.start()
        deadline = time.monotonic() + 120
        proof_path = None
        while time.monotonic() < deadline:
            journal = worker.journal.read()
            if journal and journal['phase'] == 'running':
                attempt = journal['assignment']['attempt_id']
                candidate = Path(config['workspace'])/attempt/'output'/'container.json'
                if candidate.exists():
                    proof_path = candidate
                    break
            assert worker_thread.is_alive(), (
                f'worker ended before rootless container started: {worker_errors!r}')
            time.sleep(.1)
        assert proof_path is not None, 'rootless container did not start before the deadline'
        proof = json.loads(proof_path.read_text())
        container_pid = proof['pid']
        unit = worker.executor.unit(attempt)
        state = worker.executor.inspect(attempt)
        assert state['UnitManager'] == '--system', state
        group = state.get('ControlGroup')
        assert group and proof['group'].startswith(group+'/'), proof
        assert runtime_path(unit).is_dir(), 'attempt-scoped rootless runtime/socket is missing'

        # Advance authority time past the granted lease. The real HTTP heartbeat
        # fences the attempt; its systemd unit must stop before cleanup is acked.
        clock_offset[0] = lease_seconds+1
        worker_thread.join(timeout=30)
        assert not worker_thread.is_alive(), 'expired task-environment worker did not stop'
        assert worker_errors == []
        worker.reconcile()

        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        assert not cgroup.exists() or 'populated 0' in (cgroup/'cgroup.events').read_text()
        assert not Path('/proc', str(container_pid)).exists(), 'rootless container survived lease expiry'
        assert not runtime_path(unit).exists(), 'attempt-scoped socket/runtime survived cleanup'
        assert worker.journal.read() is None
        assert list(Path(config['workspace']).iterdir()) == []
        marker = json.loads((environment_root/handle/'environment.json').read_text())
        assert marker['state'] == 'rebuild_required'
        assert (environment_root/handle/'source'/'build'/'cache.txt').exists()
        assert not any(replica['handle'] == handle for replica in worker.task_environments.report()[1])
        assert caller.get_environment(handle)['state'] == 'rebuild_required'
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM attempts WHERE state IN ('running','cleanup')").fetchone()[0] == 0
            assert db.execute("SELECT ready FROM workers WHERE id='taskenv-docker'").fetchone()[0]
    finally:
        if worker_thread and worker_thread.is_alive():
            clock_offset[0] = lease_seconds+1
            if job is not None:
                caller.request('jobs/'+job['id']+'/cancel', {})
        if attempt:
            worker.executor.stop(attempt)
        if worker_thread:
            worker_thread.join(timeout=30)
        worker.close()
        caller.close()
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()
