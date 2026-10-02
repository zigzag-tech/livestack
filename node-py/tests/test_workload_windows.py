"""Real Job Objects, a real worker and a real HTTP authority on Windows.

openspec/changes/windows-host-worker. Requires a Windows host (any user that may
create `Global\\` named objects: an administrator, or a service account). Never
touches an enrolled worker: every job name is derived from a disposable worker
id, and the authority is an in-process server on a temporary store.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows Job Object controls')

from livestack_node.workloads.archive import capture  # noqa: E402
from livestack_node.workloads.client import WorkloadClient  # noqa: E402
from livestack_node.workloads.http import Principal, WorkloadServer  # noqa: E402
from livestack_node.workloads.model import WorkloadError  # noqa: E402
from livestack_node.workloads.store import WorkloadStore  # noqa: E402
from livestack_node.workloads.supervision import WorkerJournal  # noqa: E402
from livestack_node.workloads.transfer import InputTransfer  # noqa: E402

if sys.platform == 'win32':
    from livestack_node.workloads import windows_proc
    from livestack_node.workloads.windows_supervision import JobObjectExecutor
    from livestack_node.workloads.worker import WorkloadWorker

ENV = {k: v for k, v in os.environ.items() if k.upper() in ('SYSTEMROOT', 'PATH', 'WINDIR', 'COMSPEC')}
# A handler that starts a child, which starts a detached grandchild, then waits.
TREE = '''import subprocess,sys,time
if len(sys.argv) == 1:
    subprocess.Popen([sys.executable, __file__, 'child'])
elif sys.argv[1] == 'child':
    subprocess.Popen([sys.executable, __file__, 'grandchild'], creationflags=0x00000008|0x00000200)
time.sleep(300)
'''


def until(predicate, seconds=15):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('condition did not become true')


@pytest.fixture
def executor():
    executor = JobObjectExecutor('windows-test-'+uuid.uuid4().hex[:12])
    started = []
    original = executor.start

    def start(attempt, *args, **kwargs):
        started.append(attempt)
        return original(attempt, *args, **kwargs)
    executor.start = start
    yield executor
    for attempt in started:
        executor.stop(attempt)


def start(executor, tmp_path, code, *, memory_bytes=256*1024**2, tasks=16, max_seconds=60, cpu=1):
    attempt = uuid.uuid4().hex
    script = tmp_path/(attempt+'.py')
    script.write_text(code)
    executor.start(attempt, [sys.executable, str(script)], tmp_path, tmp_path/attempt, env=ENV, cpu=cpu,
                   memory_bytes=memory_bytes, max_seconds=max_seconds, tasks=tasks)
    return attempt, tmp_path/attempt


def test_stop_kills_child_and_detached_grandchild(executor, tmp_path):
    attempt, _ = start(executor, tmp_path, TREE)
    job = windows_proc.Job.open(executor.unit(attempt))
    try:
        # wrapper + handler + child + detached grandchild
        pids = until(lambda: (lambda p: p if len(p) >= 4 else None)(job.pids()))
    finally:
        job.close()
    assert executor.alive(attempt)
    executor.stop(attempt)
    assert executor.inspect(attempt)['LoadState'] == 'not-found'
    assert not [p for p in pids if windows_proc.pid_alive(p)]


def test_kernel_holds_the_attempt_limits(executor, tmp_path):
    attempt, _ = start(executor, tmp_path, 'import time; time.sleep(300)', memory_bytes=300*1024**2,
                       tasks=7, cpu=2)
    job = windows_proc.Job.open(executor.unit(attempt))
    try:
        limits = job.limits()
    finally:
        job.close()
    assert limits['memory_bytes'] == 300*1024**2
    assert limits['tasks'] == 7
    assert limits['cpu_rate'] == int(2/os.cpu_count()*10000)
    assert limits['kill_on_close']


def test_memory_breach_kills_the_tree_and_records_oom(executor, tmp_path):
    # 600 MiB of committed memory in a 256 MiB job, plus a child that would outlive it.
    attempt, output = start(executor, tmp_path, '''import subprocess,sys,time
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
blocks = []
try:
    for _ in range(60):
        blocks.append(bytearray(10*1024*1024))
except MemoryError:
    time.sleep(300)
time.sleep(300)
''')
    receipt = until(lambda: executor.exit_result(output), 30)
    assert receipt['resources']['oom_kill'] == 1
    assert receipt['exit_code'] != 0
    # The kernel charges commit in its own granules: the peak may pass the
    # limit by under a MiB, never by the 600 MiB the handler asked for.
    assert receipt['resources']['memory_peak_bytes'] < 260*1024**2
    until(lambda: not executor.alive(attempt))


def test_task_cap_refuses_spawns_and_records_the_event(executor, tmp_path):
    attempt, output = start(executor, tmp_path, '''import subprocess,sys
children, refused = [], 0
for _ in range(8):
    try:
        children.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']))
    except OSError:
        refused += 1
print('refused', refused, flush=True)
for c in children: c.kill()
raise SystemExit(0 if refused else 3)
''', tasks=4)
    receipt = until(lambda: executor.exit_result(output), 30)
    assert receipt['exit_code'] == 0, (output/'command.log').read_text()
    assert receipt['resources']['pids_max_events'] == 1


def test_wall_time_is_enforced(executor, tmp_path):
    attempt, output = start(executor, tmp_path, 'import time; time.sleep(300)', max_seconds=2)
    receipt = until(lambda: executor.exit_result(output), 30)
    assert receipt['exit_code'] != 0


def test_restarted_worker_finds_the_attempt_by_name(tmp_path):
    first = JobObjectExecutor('windows-restart-'+uuid.uuid4().hex[:12])
    attempt, _ = start(first, tmp_path, TREE)
    pids = until(lambda: (lambda j: (lambda p: p if len(p) >= 4 else None)(j.pids()))(first.jobs[attempt]))
    # The worker process goes away: its handle closes, the wrapper's keeps the job.
    first.jobs.pop(attempt).close()
    second = JobObjectExecutor(first.worker_id)
    state = second.inspect(attempt)
    assert (state['LoadState'], state['ActiveState']) == ('loaded', 'active')
    assert int(state['ActiveProcesses']) >= len(pids)
    second.stop(attempt)
    assert second.inspect(attempt)['LoadState'] == 'not-found'
    assert not [p for p in pids if windows_proc.pid_alive(p)]


def test_finished_attempt_writes_receipt_and_log(executor, tmp_path):
    attempt, output = start(executor, tmp_path, 'print("hello from the job"); raise SystemExit(7)')
    receipt = until(lambda: executor.exit_result(output))
    assert receipt['exit_code'] == 7
    assert receipt['resources']['oom_kill'] == 0
    assert 'hello from the job' in (output/'command.log').read_text()
    executor.stop(attempt)
    assert executor.inspect(attempt)['LoadState'] == 'not-found'


def test_second_journal_on_the_same_slot_is_refused(tmp_path):
    journal = WorkerJournal(tmp_path/'state')
    try:
        code = ('import sys; sys.path.insert(0, %r)\n'
                'from livestack_node.workloads.supervision import WorkerJournal\n'
                'from livestack_node.workloads.model import WorkloadError\n'
                'try:\n    WorkerJournal(%r)\nexcept WorkloadError as e:\n    print(e.status)\n'
                % (str(Path(__file__).resolve().parents[1]), str(tmp_path/'state')))
        reply = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=30)
        assert reply.stdout.strip() == '409', reply.stderr
        journal.write({'assignment': {'attempt_id': 'x'}})
        assert journal.read() == {'assignment': {'attempt_id': 'x'}}
    finally:
        journal.close()


@pytest.fixture
def fleet(tmp_path):
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'native.v1', 'harmony.probe.v1'})
    worker_id = 'windows-integration-'+uuid.uuid4().hex[:8]
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('owner', 'a'*32, 'caller', ('native.v1', 'harmony.probe.v1')),
        Principal('worker', 'w'*32, 'worker', worker=worker_id, host='test-host')])
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = tmp_path/'installed-handler.py'
    script.write_text('''import json,os
from pathlib import Path
request=json.loads(Path(os.environ['HARMONY_REQUEST']).read_text())
Path(os.environ['HARMONY_OUTPUT'],'artifact').write_text(Path('input').read_text()+'|'+os.environ['TEMP'])
print('finished')
raise SystemExit(request.get('exit',0))
''')
    probe = Path(__file__).resolve().parents[1]/'livestack_node/workloads/probe.py'
    url = f'http://127.0.0.1:{server.server_port}'
    config = dict(authority=url, token='w'*32, worker=worker_id, state_dir=str(tmp_path/'state'),
        workspace=str(tmp_path/'workspace'), require_dedicated_filesystem=False, lease_interval=.2,
        capacity={'cpu': 1, 'memory_bytes': 512*1024**2, 'disk_bytes': 64*1024**2},
        memory_reserve_bytes=0, disk_reserve_bytes=0, environment=ENV,
        handlers={'native.v1': dict(argv=[sys.executable, str(script)], outputs=['artifact']),
                  'harmony.probe.v1': dict(argv=[sys.executable, str(probe)], outputs=['probe.json'])})
    caller = WorkloadClient(url, 'a'*32)
    source = tmp_path/'source'
    source.mkdir()
    (source/'input').write_text('captured bytes')
    capture(source, ['input'], tmp_path/'source.tar')
    digest = InputTransfer(caller).put(tmp_path/'source.tar')['digest']
    try:
        yield config, caller, digest
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_worker_runs_a_job_end_to_end_inside_a_job_object(fleet, tmp_path):
    config, caller, digest = fleet
    worker = WorkloadWorker(config)
    try:
        report = worker.report()
        assert 'host' not in report
        memory = windows_proc.memory_status()
        assert 0 < report['available']['memory_bytes'] <= min(512*1024**2, memory['available'])
        job = caller.submit(dict(version=1, key='one', handler='native.v1', input_digest=digest,
                                 need={'cpu': .5, 'memory_bytes': 256*1024**2, 'disk_bytes': 1024**2},
                                 payload={'exit': 0}))
        # A busy host (this one also runs a WSL e2e worker) may offer too
        # little CPU for a moment: placement waits, so does the test.
        until(worker.step, 60)
        result = caller.get(job['id'])
        assert result['state'] == 'succeeded', result
        receipt = result['result']['result']
        assert receipt['resources']['memory_nonreclaimable_peak_bytes'] > 0
        artifact = next(a for a in receipt['artifacts'] if a['name'] == 'artifact')
        text = InputTransfer(caller).get(artifact['digest'], tmp_path/'returned').read_text()
        value, temp = text.split('|')
        assert value == 'captured bytes'
        assert temp.endswith('tmp') and job['id'] not in temp
        assert worker.journal.read() is None
        assert [p.name for p in Path(config['workspace']).iterdir() if not p.name.startswith('input-cache')] == []
    finally:
        worker.close()


def test_probe_reports_the_job_object_limits(fleet, tmp_path):
    config, caller, digest = fleet
    worker = WorkloadWorker(config)
    try:
        job = caller.submit(dict(version=1, key='probe', handler='harmony.probe.v1', input_digest=digest,
                                 need={'cpu': .5, 'memory_bytes': 256*1024**2, 'disk_bytes': 1024**2},
                                 payload={}))
        # A busy host (this one also runs a WSL e2e worker) may offer too
        # little CPU for a moment: placement waits, so does the test.
        until(worker.step, 60)
        result = caller.get(job['id'])
        assert result['state'] == 'succeeded', result
        artifact = next(a for a in result['result']['result']['artifacts'] if a['name'] == 'probe.json')
        probe = json.loads(InputTransfer(caller).get(artifact['digest'], tmp_path/'probe.json').read_text())
        assert probe['isolation'] == 'windows-job-object'
        assert probe['memory_max'] == str(256*1024**2)
        # CpuRate is 1/100 % of the machine: the quota is the nearest step to 0.5 CPU.
        n = os.cpu_count()
        assert probe['cpu_max'] == '%d 100000' % round(int(.5/n*10000)/10000*n*100000)
    finally:
        worker.close()


def test_read_only_source_tree_is_removed(tmp_path):
    from livestack_node.workloads.worker import rmtree_writable
    tree = tmp_path/'attempt'
    (tree/'source').mkdir(parents=True)
    (tree/'source'/'locked.txt').write_text('x')
    os.chmod(tree/'source'/'locked.txt', 0o444)
    rmtree_writable(tree)
    assert not tree.exists()


def _sc(*args):
    return subprocess.run(['sc.exe', *args], capture_output=True, text=True, timeout=60)


def _service_state(name):
    reply = _sc('query', name)
    for line in reply.stdout.splitlines():
        if 'STATE' in line:
            return line.split()[-1]
    return None


@pytest.fixture
def service(tmp_path):
    if subprocess.run(['net', 'session'], capture_output=True).returncode:
        pytest.skip('creating a service needs an administrator')
    name = 'LivestackWorkerTest' + uuid.uuid4().hex[:8]
    node_py = Path(__file__).resolve().parents[1]

    def create(config):
        path = tmp_path/'worker.json'
        path.write_text(json.dumps(config))
        python = getattr(sys, '_base_executable', sys.executable)
        command = (f'"{python}" -m livestack_node.workloads.windows_service '
                   f'--config "{path}" --service-name {name}')
        assert _sc('create', name, 'binPath=', command, 'start=', 'demand').returncode == 0
        subprocess.run(['reg', 'add', rf'HKLM\SYSTEM\CurrentControlSet\Services\{name}', '/v', 'Environment',
                        '/t', 'REG_MULTI_SZ', '/d', f'PYTHONPATH={node_py}\\0PYTHONDONTWRITEBYTECODE=1', '/f'],
                       check=True, capture_output=True)
        _sc('failure', name, 'reset=', '60', 'actions=', 'restart/1000/restart/1000/restart/1000')
        return name
    yield create
    _sc('stop', name)
    until(lambda: _service_state(name) in (None, 'STOPPED'), 30)
    _sc('delete', name)


def test_service_runs_the_worker_and_stops_cleanly(service, tmp_path):
    # An authority nobody listens on: the worker runs and waits, as on a network outage.
    name = service(dict(authority='http://127.0.0.1:9', token='w'*32, worker='svc-test',
                        state_dir=str(tmp_path/'state'), workspace=str(tmp_path/'workspace'),
                        require_dedicated_filesystem=False, environment=ENV,
                        handlers={'native.v1': dict(argv=[sys.executable, '-c', 'pass'])}))
    assert _sc('start', name).returncode == 0
    until(lambda: _service_state(name) == 'RUNNING', 30)
    log = until(lambda: (lambda p: p.exists() and 'worker waiting after' in p.read_text() and p)(
        tmp_path/'state'/'worker.log'), 30)
    assert 'windows worker starting' in log.read_text()
    assert _sc('stop', name).returncode == 0
    until(lambda: _service_state(name) == 'STOPPED', 30)


def test_service_whose_worker_dies_is_restarted_by_the_scm(service, tmp_path):
    # No handlers: WorkloadWorker refuses to start, the thread dies, the process
    # fails without reporting SERVICE_STOPPED, and the failure action restarts it.
    name = service(dict(authority='http://127.0.0.1:9', token='w'*32, worker='svc-test',
                        state_dir=str(tmp_path/'state'), workspace=str(tmp_path/'workspace'),
                        require_dedicated_filesystem=False, environment=ENV, handlers={}))
    assert _sc('start', name).returncode == 0
    log = tmp_path/'state'/'worker.log'
    until(lambda: log.exists() and log.read_text().count('windows worker starting') >= 2, 60)
    assert 'service thread ended' in log.read_text()
