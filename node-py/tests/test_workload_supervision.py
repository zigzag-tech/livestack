"""Real systemd/cgroup tests: process ownership cannot be proven with a fake."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.supervision import SystemdExecutor, WorkerJournal


@pytest.fixture
def workload_test_root():
    base = Path.home()/'.cache'/'livestack-workload-tests'
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='supervision-', dir=base))
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.fixture
def executor():
    if subprocess.run(['systemctl', '--user', 'show'], capture_output=True).returncode:
        pytest.skip('requires Linux cgroup v2 and a running systemd user manager')
    return SystemdExecutor('integration-'+uuid.uuid4().hex)


def until(predicate, seconds=10):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('condition did not become true')


def test_inspect_discovers_and_caches_system_manager_unit(monkeypatch):
    executor = SystemdExecutor('manager-selection')
    attempt = uuid.uuid4().hex
    calls = []

    def inspect_manager(manager, unit):
        calls.append(manager)
        if manager == '--user':
            return {'LoadState': 'not-found'}
        return {'LoadState': 'loaded', 'ActiveState': 'active', 'ControlGroup': '/system.slice/job',
                'UnitManager': manager}

    monkeypatch.setattr(executor, '_inspect_manager', inspect_manager)
    state = executor.inspect(attempt)
    assert state['UnitManager'] == '--system'
    assert calls == ['--user', '--system']
    assert executor.inspect(attempt)['UnitManager'] == '--system'
    assert calls == ['--user', '--system', '--system']


def test_stop_routes_system_manager_unit_through_sudo(monkeypatch):
    executor = SystemdExecutor('manager-stop')
    attempt = uuid.uuid4().hex
    states = iter([
        {'LoadState': 'loaded', 'ActiveState': 'active', 'UnitManager': '--system'},
        {'LoadState': 'loaded', 'ActiveState': 'inactive', 'UnitManager': '--system'},
    ])
    monkeypatch.setattr(executor, 'inspect', lambda _attempt: next(states))
    commands = []

    def command(*args, check=True):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

    monkeypatch.setattr(executor, 'command', command)
    monkeypatch.setattr('livestack_node.workloads.supervision.docker_runtime.cleanup', lambda _unit: None)
    executor.stop(attempt)
    assert commands == [
        ('/usr/bin/sudo', '-n', '/usr/bin/systemctl', '--system', 'stop', executor.unit(attempt)),
        ('/usr/bin/sudo', '-n', '/usr/bin/systemctl', '--system', 'reset-failed', executor.unit(attempt)),
    ]


def test_compilation_execution_uses_system_manager_and_worker_identity(monkeypatch, workload_test_root):
    executor = SystemdExecutor('compilation-manager-selection')
    attempt = uuid.uuid4().hex
    monkeypatch.setattr(executor, 'inspect', lambda _attempt: {'LoadState': 'not-found'})
    commands = []

    def command(*args, check=True):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

    monkeypatch.setattr(executor, 'command', command)
    executor.start(attempt, ['/usr/bin/python3', '-c', 'pass'], workload_test_root,
        workload_test_root/'compilation-out', env={}, cpu=.5, memory_bytes=128*1024**2,
        max_seconds=30, tasks=64, host_identity_required=True)
    args = commands[0]
    assert args[:4] == ('/usr/bin/sudo', '-n', '/usr/bin/systemd-run', '--system')
    assert '--user' not in args
    assert '--property=User='+str(os.getuid()) in args
    assert '--property=Group='+str(os.getgid()) in args
    assert '--property=PrivateTmp=yes' in args
    assert '--property=NoNewPrivileges=yes' in args
    assert '--property=MemoryMax='+str(128*1024**2) in args
    assert '--property=TasksMax=64' in args
    assert '--property=RuntimeMaxSec=30' in args
    assert '--property=CPUQuota=50.0%' in args


def test_runtime_only_execution_stays_on_user_manager(monkeypatch, workload_test_root):
    executor = SystemdExecutor('runtime-manager-selection')
    attempt = uuid.uuid4().hex
    monkeypatch.setattr(executor, 'inspect', lambda _attempt: {'LoadState': 'not-found'})
    commands = []

    def command(*args, check=True):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

    monkeypatch.setattr(executor, 'command', command)
    executor.start(attempt, ['/usr/bin/python3', '-c', 'pass'], workload_test_root,
        workload_test_root/'runtime-out', env={}, cpu=.5, memory_bytes=128*1024**2)
    assert commands[0][0:2] == ('systemd-run', '--user')
    assert '--system' not in commands[0]
    assert not any(value.startswith('--property=User=') for value in commands[0])
    assert not any(value.startswith('--property=Group=') for value in commands[0])


def test_compilation_execution_fails_closed_when_system_manager_is_unavailable(monkeypatch, workload_test_root):
    executor = SystemdExecutor('compilation-no-manager-fallback')
    attempt = uuid.uuid4().hex
    monkeypatch.setattr(executor, 'inspect', lambda _attempt: {'LoadState': 'not-found'})
    commands = []

    def unavailable(*args, check=True):
        commands.append(args)
        raise subprocess.CalledProcessError(1, args, stderr='sudo not permitted')

    monkeypatch.setattr(executor, 'command', unavailable)
    with pytest.raises(WorkloadError, match='compilation_host_identity_unavailable'):
        executor.start(attempt, ['/usr/bin/python3', '-c', 'pass'], workload_test_root,
            workload_test_root/'compilation-refused', env={}, cpu=.5, memory_bytes=128*1024**2,
            host_identity_required=True)
    assert len(commands) == 1
    assert commands[0][:4] == ('/usr/bin/sudo', '-n', '/usr/bin/systemd-run', '--system')
    assert '--user' not in commands[0]


def test_compilation_host_identity_requires_a_boolean(workload_test_root, executor):
    with pytest.raises(WorkloadError, match='host identity requirement must be a boolean'):
        executor.start(uuid.uuid4().hex, ['/usr/bin/python3', '-c', 'pass'], workload_test_root,
            workload_test_root/'invalid-identity-flag', env={}, cpu=.1, memory_bytes=64*1024**2,
            host_identity_required='true')


def test_restart_journal_stops_grandchildren_and_limits_resources(workload_test_root, executor):
    attempt = uuid.uuid4().hex
    root = workload_test_root/'journal'
    journal = WorkerJournal(root)
    try:
        with pytest.raises(WorkloadError, match='already supervised'):
            WorkerJournal(root)
        journal.write({'attempt_id': attempt})
        executor.start(attempt, [sys.executable, '-c',
            'import subprocess,time; subprocess.Popen(["sleep","120"]); time.sleep(120)'],
            workload_test_root, workload_test_root/'out', env=dict(os.environ), cpu=.5, memory_bytes=128*1024**2,
            max_seconds=120, tasks=32)
        group = until(lambda: executor.inspect(attempt).get('ControlGroup'))
        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        until(lambda: len((cgroup/'cgroup.procs').read_text().split()) >= 3)
        assert (cgroup/'memory.max').read_text().strip() == str(128*1024**2)
        assert (cgroup/'pids.max').read_text().strip() == '32'
        quota, period = map(int, (cgroup/'cpu.max').read_text().split())
        assert quota/period == .5
        journal.close()
        journal = WorkerJournal(root)  # Supervisor restarted; no PID assumption.
        executor.stop(journal.read()['attempt_id'])
        assert not cgroup.exists() or 'populated 0' in (cgroup/'cgroup.events').read_text()
        journal.clear()
        assert journal.read() is None
    finally:
        executor.stop(attempt)
        journal.close()



def test_cleanup_accepts_unit_removed_between_inspection_and_stop(workload_test_root, executor):
    # The unit and competing stop are real. Inject only their ordering so the
    # cleanup race is deterministic instead of a probabilistic timing test.
    attempt = uuid.uuid4().hex
    executor.start(attempt, ['/bin/sleep', '30'], workload_test_root, workload_test_root/'out',
                   env=dict(os.environ), cpu=.1, memory_bytes=64*1024**2)
    inspect = executor.inspect
    observed = False
    def inspect_then_remove(value):
        nonlocal observed
        result = inspect(value)
        if not observed:
            observed = True
            executor.command('systemctl', '--user', 'stop', executor.unit(value))
            executor.command('systemctl', '--user', 'reset-failed', executor.unit(value), check=False)
            until(lambda: inspect(value).get('LoadState') == 'not-found')
        return result
    executor.inspect = inspect_then_remove
    try:
        executor.stop(attempt)
        assert observed
        assert inspect(attempt)['LoadState'] == 'not-found'
    finally:
        executor.inspect = inspect
        executor.stop(attempt)

def test_output_is_drained_but_storage_is_bounded(workload_test_root, executor):
    attempt = uuid.uuid4().hex
    out = workload_test_root/'out'
    try:
        executor.start(attempt, [sys.executable, '-c',
            'import sys; sys.stdout.write("x"*1000000); sys.exit(7)'],
            workload_test_root, out, env=dict(os.environ), cpu=1, memory_bytes=128*1024**2, log_bytes=4096)
        result = until(lambda: executor.exit_result(out))
        assert result['exit_code'] == 7
        assert result['resources']['pids_max_events'] == 0
        assert result['resources']['cpu_usage_usec'] > 0
        assert sum(p.stat().st_size for p in out.glob('*.log')) <= 8192
    finally:
        executor.stop(attempt)


def test_attempt_has_private_writable_tmp_with_no_new_privileges(workload_test_root, executor):
    attempt = uuid.uuid4().hex
    marker = 'harmony-test-'+uuid.uuid4().hex
    host_markers = [Path('/tmp/.X11-unix')/marker, Path('/var/tmp')/marker]
    program = f"""from pathlib import Path
import re, time
assert re.search(r'^NoNewPrivs:\\s+1$', Path('/proc/self/status').read_text(), re.M)
directory = Path('/tmp/.X11-unix')
directory.mkdir(parents=True, exist_ok=True)
(directory/{marker!r}).write_text('private')
(Path('/var/tmp')/{marker!r}).write_text('private')
time.sleep(1)
"""
    try:
        executor.start(attempt, [sys.executable, '-c', program], workload_test_root,
            workload_test_root/'out', env=dict(os.environ), cpu=.1, memory_bytes=128*1024**2)
        properties = executor.command('systemctl', '--user', 'show', executor.unit(attempt),
            '--property=PrivateTmp,NoNewPrivileges').stdout.splitlines()
        assert dict(line.split('=', 1) for line in properties) == {
            'PrivateTmp': 'yes', 'NoNewPrivileges': 'yes'}
        result = until(lambda: executor.exit_result(workload_test_root/'out'))
        assert result['exit_code'] == 0, result
        assert all(not marker.exists() for marker in host_markers)
    finally:
        executor.stop(attempt)
        for marker_path in host_markers:
            marker_path.unlink(missing_ok=True)


def test_compilation_attempt_keeps_host_verifier_identity_and_limits(workload_test_root, executor):
    registry = Path('/etc/livestack/compilation-launch.json')
    if not registry.is_file():
        pytest.skip('requires the enrolled host compilation verifier registry')
    if not Path('/usr/bin/sudo').is_file() or subprocess.run(
            ['/usr/bin/sudo', '-n', 'true'], capture_output=True).returncode:
        pytest.skip('requires the enrolled worker system-manager sudo rule')
    if subprocess.run(['systemctl', '--system', 'show'], capture_output=True).returncode:
        pytest.skip('requires a running system systemd manager')

    attempt = uuid.uuid4().hex
    out = workload_test_root/'compilation-identity'
    out.mkdir()
    report_path = out/'identity.json'
    marker = 'harmony-compilation-'+uuid.uuid4().hex
    host_markers = [Path('/tmp/.X11-unix')/marker, Path('/var/tmp')/marker]
    program = f"""import json, os, re, time
from pathlib import Path
status = Path('/proc/self/status').read_text()
cgroup = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
              if line.startswith('0::'))
report = dict(uid=os.getuid(), gid=os.getgid(),
              registry_uid=Path({str(registry)!r}).stat().st_uid,
              registry_gid=Path({str(registry)!r}).stat().st_gid,
              no_new_privileges=bool(re.search(r'^NoNewPrivs:\\s+1$', status, re.M)),
              cgroup=cgroup)
Path({str(report_path)!r}).write_text(json.dumps(report))
directory = Path('/tmp/.X11-unix')
directory.mkdir(parents=True, exist_ok=True)
(directory/{marker!r}).write_text('private')
(Path('/var/tmp')/{marker!r}).write_text('private')
time.sleep(1.5)
"""
    try:
        executor.start(attempt, ['/usr/bin/python3', '-c', program], workload_test_root, out,
            env={}, cpu=.1, memory_bytes=128*1024**2, max_seconds=30, tasks=32,
            host_identity_required=True)
        group = until(lambda: executor.inspect(attempt).get('ControlGroup'))
        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        assert (cgroup/'memory.max').read_text().strip() == str(128*1024**2)
        assert (cgroup/'pids.max').read_text().strip() == '32'
        quota, period = map(int, (cgroup/'cpu.max').read_text().split())
        assert quota/period == pytest.approx(.1)
        properties = subprocess.run(['/usr/bin/sudo', '-n', '/usr/bin/systemctl', '--system',
            'show', executor.unit(attempt), '--property=PrivateTmp,NoNewPrivileges'],
            check=True, capture_output=True, text=True).stdout.splitlines()
        assert dict(line.split('=', 1) for line in properties) == {
            'PrivateTmp': 'yes', 'NoNewPrivileges': 'yes'}
        result = until(lambda: executor.exit_result(out))
        report = json.loads(report_path.read_text())
        assert result['exit_code'] == 0, result
        assert report['uid'] == os.getuid()
        assert report['gid'] == os.getgid()
        assert report['registry_uid'] == 0
        assert report['registry_gid'] == 0
        assert report['no_new_privileges'] is True
        assert group in report['cgroup']
        assert all(not marker.exists() for marker in host_markers)
    finally:
        executor.stop(attempt)
        for marker_path in host_markers:
            marker_path.unlink(missing_ok=True)


def test_environment_execution_sees_only_its_bound_source_tree(workload_test_root, executor):
    attempt = uuid.uuid4().hex
    environment_root = workload_test_root/'shared-environments'
    source = environment_root/'handle-a'/'source'
    other = environment_root/'handle-b'/'source'
    view = workload_test_root/'attempt'/'environment-view'
    source.mkdir(parents=True)
    other.mkdir(parents=True)
    view.mkdir(parents=True)
    (source/'captured').write_text('current-task')
    (other/'secret').write_text('different-owner')
    program = f"""from pathlib import Path
assert Path({str(view/'captured')!r}).read_text() == 'current-task'
try:
    Path({str(other/'secret')!r}).read_text()
except PermissionError:
    pass
else:
    raise AssertionError('sibling environment was readable')
"""
    try:
        executor.start(attempt, [sys.executable, '-c', program], view, workload_test_root/'out',
            env=dict(os.environ), cpu=.1, memory_bytes=128*1024**2,
            inaccessible_paths=[str(environment_root)], bind_paths=[(str(source), str(view))])
        result = until(lambda: executor.exit_result(workload_test_root/'out'))
        assert result['exit_code'] == 0, result
    finally:
        executor.stop(attempt)


@pytest.mark.parametrize('tasks', [0, .5, True, 8193])
def test_task_budget_cannot_disable_or_escape_the_bound(workload_test_root, executor, tasks):
    with pytest.raises(WorkloadError):
        executor.start(uuid.uuid4().hex, [sys.executable, '-c', 'pass'], workload_test_root,
                       workload_test_root/'out',
                       env=dict(os.environ), cpu=1, memory_bytes=128*1024**2, tasks=tasks)


@pytest.mark.parametrize('close_output', [False, True])
def test_expired_lease_stops_job_without_supervisor(workload_test_root, executor, close_output):
    attempt = uuid.uuid4().hex
    lease = workload_test_root/'lease'
    lease.write_text(str(time.monotonic()+1))
    try:
        program = ('import os,subprocess,time; subprocess.Popen(["sleep","120"], '
                   'stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); ')
        if close_output:
            program += 'os.close(1); os.close(2); '
        program += 'time.sleep(120)'
        executor.start(attempt, [sys.executable, '-c', program],
            workload_test_root, workload_test_root/'out', env=dict(os.environ), cpu=1, memory_bytes=128*1024**2,
            lease_file=lease)
        group = until(lambda: executor.inspect(attempt).get('ControlGroup'))
        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        until(lambda: not cgroup.exists() or 'populated 0' in (cgroup/'cgroup.events').read_text())
        assert executor.exit_result(workload_test_root/'out') is None
    finally:
        executor.stop(attempt)
