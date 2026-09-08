"""Real systemd/cgroup tests: process ownership cannot be proven with a fake."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.supervision import SystemdExecutor, WorkerJournal


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


def test_restart_journal_stops_grandchildren_and_limits_resources(tmp_path, executor):
    attempt = uuid.uuid4().hex
    root = tmp_path/'journal'
    journal = WorkerJournal(root)
    try:
        with pytest.raises(WorkloadError, match='already supervised'):
            WorkerJournal(root)
        journal.write({'attempt_id': attempt})
        executor.start(attempt, [sys.executable, '-c',
            'import subprocess,time; subprocess.Popen(["sleep","120"]); time.sleep(120)'],
            tmp_path, tmp_path/'out', env=dict(os.environ), cpu=.5, memory_bytes=128*1024**2,
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



def test_cleanup_accepts_unit_removed_between_inspection_and_stop(tmp_path, executor):
    # The unit and competing stop are real. Inject only their ordering so the
    # cleanup race is deterministic instead of a probabilistic timing test.
    attempt = uuid.uuid4().hex
    executor.start(attempt, ['/bin/sleep', '30'], tmp_path, tmp_path/'out',
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

def test_output_is_drained_but_storage_is_bounded(tmp_path, executor):
    attempt = uuid.uuid4().hex
    out = tmp_path/'out'
    try:
        executor.start(attempt, [sys.executable, '-c',
            'import sys; sys.stdout.write("x"*1000000); sys.exit(7)'],
            tmp_path, out, env=dict(os.environ), cpu=1, memory_bytes=128*1024**2, log_bytes=4096)
        result = until(lambda: executor.exit_result(out))
        assert result['exit_code'] == 7
        assert result['resources']['pids_max_events'] == 0
        assert result['resources']['cpu_usage_usec'] > 0
        assert sum(p.stat().st_size for p in out.glob('*.log')) <= 8192
    finally:
        executor.stop(attempt)


@pytest.mark.parametrize('tasks', [0, .5, True, 8193])
def test_task_budget_cannot_disable_or_escape_the_bound(tmp_path, executor, tasks):
    with pytest.raises(WorkloadError):
        executor.start(uuid.uuid4().hex, [sys.executable, '-c', 'pass'], tmp_path, tmp_path/'out',
                       env=dict(os.environ), cpu=1, memory_bytes=128*1024**2, tasks=tasks)


@pytest.mark.parametrize('close_output', [False, True])
def test_expired_lease_stops_job_without_supervisor(tmp_path, executor, close_output):
    attempt = uuid.uuid4().hex
    lease = tmp_path/'lease'
    lease.write_text(str(time.monotonic()+1))
    try:
        program = ('import os,subprocess,time; subprocess.Popen(["sleep","120"], '
                   'stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); ')
        if close_output:
            program += 'os.close(1); os.close(2); '
        program += 'time.sleep(120)'
        executor.start(attempt, [sys.executable, '-c', program],
            tmp_path, tmp_path/'out', env=dict(os.environ), cpu=1, memory_bytes=128*1024**2,
            lease_file=lease)
        group = until(lambda: executor.inspect(attempt).get('ControlGroup'))
        cgroup = Path('/sys/fs/cgroup')/group.lstrip('/')
        until(lambda: not cgroup.exists() or 'populated 0' in (cgroup/'cgroup.events').read_text())
        assert executor.exit_result(tmp_path/'out') is None
    finally:
        executor.stop(attempt)
