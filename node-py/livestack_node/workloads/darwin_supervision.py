"""Durable ownership and supervision for one macOS worker slot (launchd).

The SystemdExecutor interface, on launchd: one job per attempt in the worker
user's GUI domain (Xcode, codesign and the login keychain need the Aqua
session), labelled deterministically so a restarted worker finds it. launchd
runs the job in its own process group and kills that group on removal; the
bounded wrapper inside it enforces memory/tasks/wall time (macOS has no
cgroups). Design: openspec/changes/apple-host-compilation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import sys
import time

from . import darwin_proc
from .model import WorkloadError, encode

LABEL_PREFIX = 'io.livestack.harmony-work.'
STOP_GRACE_SECONDS = 5


def job_label(worker_id, attempt_id):
    if not re.fullmatch('[a-f0-9]{32}', attempt_id):
        raise WorkloadError('invalid attempt identity')
    return LABEL_PREFIX + hashlib.sha256(worker_id.encode()).hexdigest()[:16] + '.' + attempt_id


def launchd_job(domain, label, timeout=10):
    """{'state', 'pid', 'arguments'} launchd holds for the job, or None when
    launchd has no such job. Parsed from `launchctl print`, whose text format is
    not an API: anything unparseable raises instead of reading as "absent"."""
    reply = subprocess.run(['/bin/launchctl', 'print', f'{domain}/{label}'],
                           capture_output=True, text=True, timeout=timeout)
    if reply.returncode == 113 or 'Could not find service' in reply.stderr + reply.stdout:
        return None
    if reply.returncode != 0:
        raise WorkloadError('launchctl print failed: '+(reply.stderr or reply.stdout)[-512:], 503)
    text = reply.stdout
    if len(text.encode()) > 65536:
        raise WorkloadError('launchd job report oversized', 503)
    state = re.search(r'^\tstate = (.+)$', text, re.M)
    if state is None:
        raise WorkloadError('launchd job report has no state', 503)
    pid = re.search(r'^\tpid = (\d+)$', text, re.M)
    arguments = re.search(r'^\targuments = \{\n((?:\t\t.*\n)*?)\t\}$', text, re.M)
    return dict(state=state.group(1).strip(), pid=int(pid.group(1)) if pid else None,
                arguments=[line[2:] for line in arguments.group(1).splitlines()] if arguments else [])


class LaunchdExecutor:
    def __init__(self, worker_id, *, uid=None):
        self.worker_id = worker_id
        self.domain = f'gui/{os.getuid() if uid is None else uid}'

    def unit(self, attempt_id):
        return job_label(self.worker_id, attempt_id)

    def command(self, *args, check=True):
        return subprocess.run(args, check=check, capture_output=True, text=True, timeout=30)

    def inspect(self, attempt_id):
        """The SystemdExecutor shape: LoadState/ActiveState, plus MainPID."""
        job = launchd_job(self.domain, self.unit(attempt_id))
        if job is None:
            return {'LoadState': 'not-found', 'ActiveState': 'inactive'}
        running = job['state'] == 'running' and job['pid'] is not None
        return {'LoadState': 'loaded', 'ActiveState': 'active' if running else 'inactive',
                'MainPID': str(job['pid'] or 0)}

    def alive(self, attempt_id):
        return self.inspect(attempt_id).get('ActiveState') == 'active'

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
            raise WorkloadError('rootless Docker backends are not supported on macOS workers')
        label = self.unit(attempt_id)
        if launchd_job(self.domain, label) is not None:
            raise WorkloadError('attempt already has a launchd job; reconcile before launch', 409)
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        config = output/'execution.json'
        config.write_text(encode(dict(argv=argv, cwd=str(Path(cwd).resolve()), output=str(output),
                                      env=env, log_bytes=int(log_bytes),
                                      lease_file=str(lease_file) if lease_file else None)))
        config.chmod(0o600)
        wrapper = Path(__file__).with_name('bounded_exec.py').resolve()
        # The limits travel as arguments so launchd itself holds them: the root
        # verifier reads them from `launchctl print`, not from a file the
        # handler (same uid) could rewrite.
        plist = output.parent/(label+'.plist')
        plist.write_bytes(plistlib.dumps(dict(
            Label=label,
            ProgramArguments=[sys.executable, str(wrapper), str(config),
                              '--memory-bytes', str(int(memory_bytes)), '--cpu', repr(float(cpu)),
                              '--tasks', str(int(tasks)), '--max-seconds', str(int(max_seconds))],
            RunAtLoad=True, KeepAlive=False, AbandonProcessGroup=False,
            # No CPU quota exists on macOS; niced so interactive tenants keep priority.
            Nice=10, ProcessType='Standard',
            StandardOutPath='/dev/null', StandardErrorPath='/dev/null',
            WorkingDirectory=str(Path(cwd).resolve()))))
        plist.chmod(0o600)
        self.command('/bin/launchctl', 'bootstrap', self.domain, str(plist))
        # systemd-run Type=exec returns once the wrapper runs; bootstrap may
        # return while launchd still has the spawn scheduled, which the
        # worker's loop would read as "stopped without a result".
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            job = launchd_job(self.domain, label)
            if (job is not None and job['state'] == 'running') or self.exit_result(output) is not None:
                return
            time.sleep(.05)
        raise WorkloadError('launchd job did not start within 10 s', 503)

    def stop(self, attempt_id):
        """Return only after the job and every process of its tree are gone."""
        label = self.unit(attempt_id)
        job = launchd_job(self.domain, label)
        if job is None:
            return
        found = {}
        if job['pid'] is not None:
            record = darwin_proc.info(job['pid'])
            if record is not None:
                found = darwin_proc.tree(record['pid'], record['start'])
        darwin_proc.kill_tree(found, signal.SIGTERM)
        deadline = time.monotonic()+STOP_GRACE_SECONDS
        while darwin_proc.survivors(found) and time.monotonic() < deadline:
            time.sleep(.1)
        darwin_proc.kill_tree(darwin_proc.survivors(found), signal.SIGKILL)
        removed = self.command('/bin/launchctl', 'bootout', f'{self.domain}/{label}', check=False)
        deadline = time.monotonic()+STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            if launchd_job(self.domain, label) is None and not darwin_proc.survivors(found):
                return
            time.sleep(.1)
        if launchd_job(self.domain, label) is not None:
            raise WorkloadError('owned launchd job has not stopped (bootout %d); capacity remains reserved'
                                % removed.returncode, 503)
        raise WorkloadError('owned process tree still populated; capacity remains reserved', 503)

    def exit_result(self, output):
        path = Path(output)/'exit.json'
        if not path.exists():
            return None
        if path.stat().st_size > 1024:
            raise WorkloadError('oversized execution result')
        return json.loads(path.read_text())
