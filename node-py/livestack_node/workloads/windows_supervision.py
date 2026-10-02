"""Durable ownership and supervision for one Windows worker slot (Job Objects).

The SystemdExecutor interface, on Windows: one named Job Object per attempt,
named deterministically from worker and attempt so a restarted worker finds it.
The bounded wrapper is created suspended, put in the job, then resumed, so
every process of the attempt (the wrapper, the handler and every descendant)
is a member from its first instruction. The kernel enforces the job's commit
limit, process count and CPU rate; TerminateJobObject kills the whole tree.
Design: openspec/changes/windows-host-worker.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
import re
import subprocess
import sys
import time

from . import windows_proc
from .model import WorkloadError, encode

STOP_GRACE_SECONDS = 10
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


def prefix16(worker_id):
    return hashlib.sha256(worker_id.encode()).hexdigest()[:16]


def attempt_job(worker_id, attempt_id):
    if not re.fullmatch('[a-f0-9]{32}', attempt_id):
        raise WorkloadError('invalid attempt identity')
    return windows_proc.job_name(prefix16(worker_id), attempt_id)


class JobObjectExecutor:
    def __init__(self, worker_id):
        self.worker_id = worker_id
        # Handles this process holds on its attempts' jobs; the wrapper holds
        # its own, so a worker restart does not end the attempt.
        self.jobs = {}

    def unit(self, attempt_id):
        return attempt_job(self.worker_id, attempt_id)

    def command(self, *args, check=True):
        return subprocess.run(args, check=check, capture_output=True, text=True, timeout=30)

    def _open(self, attempt_id):
        job = self.jobs.get(attempt_id)
        if job is not None:
            return job, False
        return windows_proc.Job.open(self.unit(attempt_id)), True

    def inspect(self, attempt_id):
        """The SystemdExecutor shape: LoadState/ActiveState."""
        job, opened = self._open(attempt_id)
        if job is None:
            return {'LoadState': 'not-found', 'ActiveState': 'inactive'}
        try:
            active = job.accounting()['active']
        finally:
            if opened:
                job.close()
        return {'LoadState': 'loaded', 'ActiveState': 'active' if active else 'inactive',
                'ActiveProcesses': str(active)}

    def alive(self, attempt_id):
        return self.inspect(attempt_id).get('ActiveState') == 'active'

    def memory_peak(self, attempt_id):
        """The job's peak commit (private bytes: not reclaimable page cache)."""
        job = self.jobs.get(attempt_id)
        return None if job is None else job.limits()['peak_memory_bytes']

    def start(self, attempt_id, argv, cwd, output, *, env, cpu, memory_bytes,
              max_seconds=3600, tasks=512, log_bytes=8*1024**2, lease_file=None, rootless_docker=False,
              rootless_native=False, native_host_address=None):
        for value in (cpu, memory_bytes, max_seconds, tasks, log_bytes):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise WorkloadError('execution limits must be positive and finite')
        if not isinstance(tasks, int) or not 1 <= tasks <= 8192:
            raise WorkloadError('task limit must be an integer from 1 to 8192')
        if not argv or not Path(argv[0]).is_absolute():
            raise WorkloadError('installed handler must name an absolute executable')
        if rootless_docker or rootless_native:
            raise WorkloadError('rootless Docker backends are not supported on Windows workers')
        name = self.unit(attempt_id)
        if attempt_id in self.jobs or windows_proc.Job.open(name) is not None:
            raise WorkloadError('attempt already has a job object; reconcile before launch', 409)
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        config = output/'execution.json'
        config.write_text(encode(dict(argv=argv, cwd=str(Path(cwd).resolve()), output=str(output),
                                      env=env, log_bytes=int(log_bytes),
                                      lease_file=str(lease_file) if lease_file else None)))
        wrapper = Path(__file__).with_name('bounded_exec.py').resolve()
        try:
            job = windows_proc.Job.create(name, memory_bytes=memory_bytes, tasks=tasks, cpu=cpu)
        except FileExistsError:
            raise WorkloadError('attempt already has a job object; reconcile before launch', 409)
        try:
            args = [sys.executable, str(wrapper), str(config), '--job', name,
                    '--max-seconds', str(int(max_seconds))]
            flags = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            try:
                # Out of the worker's own job (a service manager's or an ssh
                # session's), so stopping the worker does not stop the attempt.
                process = subprocess.Popen(args, cwd=str(Path(cwd).resolve()), creationflags=flags |
                                           CREATE_BREAKAWAY_FROM_JOB, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except PermissionError:
                # The enclosing job forbids breakaway: the attempt's job nests
                # inside it, which still bounds and kills the attempt whole.
                process = subprocess.Popen(args, cwd=str(Path(cwd).resolve()), creationflags=flags,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
            try:
                job.assign(process._handle)
                if not job.contains(process._handle):
                    raise WorkloadError('wrapper is not inside its attempt job', 503)
                windows_proc.resume_process(process.pid)
            except BaseException:
                process.kill()
                raise
        except BaseException:
            job.terminate()
            job.close()
            raise
        self.jobs[attempt_id] = job

    def stop(self, attempt_id):
        """Return only after every process of the attempt's job is gone."""
        job, _ = self._open(attempt_id)
        if job is None:
            return
        try:
            if job.accounting()['active']:
                job.terminate()
            deadline = time.monotonic()+STOP_GRACE_SECONDS
            while job.accounting()['active'] and time.monotonic() < deadline:
                time.sleep(.1)
            active = job.accounting()['active']
            if active:
                raise WorkloadError('owned job object still has %d processes; capacity remains reserved'
                                    % active, 503)
        finally:
            job.close()
            self.jobs.pop(attempt_id, None)
        # The name outlives the last member until the dead wrapper's handle is
        # run down; wait for it so "stopped" also reads as "not found".
        deadline = time.monotonic()+STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            job = windows_proc.Job.open(self.unit(attempt_id))
            if job is None:
                return
            job.close()
            time.sleep(.1)
        logging.warning('job object %s has no processes but is still named (another handle holder)',
                        self.unit(attempt_id))

    def exit_result(self, output):
        path = Path(output)/'exit.json'
        if not path.exists():
            return None
        if path.stat().st_size > 1024:
            raise WorkloadError('oversized execution result')
        return json.loads(path.read_text())
