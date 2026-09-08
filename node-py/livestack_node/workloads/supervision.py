"""Durable ownership and cgroup supervision for one Linux/WSL worker slot.

No PID-based recovery: systemd unit names are journaled before launch and
identify the entire cgroup. Rootless Docker jobs delegate a subtree and explicitly
parent every container beneath it; host Docker daemon work is never selected.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .model import WorkloadError, encode
from . import docker_runtime


class WorkerJournal:
    """One fixed-size active record, protected by a process-lifetime lock."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.root/'worker.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise WorkloadError('worker slot already supervised', 409)
        self.path = self.root/'active.json'

    def read(self):
        if not self.path.exists():
            return None
        if self.path.stat().st_size > 65536:
            raise WorkloadError('worker journal exceeds bound; manual recovery required', 503)
        return json.loads(self.path.read_text())

    def write(self, value):
        temporary = self.root/'active.tmp'
        with temporary.open('w') as out:
            out.write(encode(value))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.path)
        self._sync()

    def clear(self):
        self.path.unlink(missing_ok=True)
        self._sync()

    def _sync(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def close(self):
        self.lock.close()


class SystemdExecutor:
    def __init__(self, worker_id):
        self.prefix = 'harmony-work-' + hashlib.sha256(worker_id.encode()).hexdigest()[:16] + '-'

    def unit(self, attempt_id):
        if not re.fullmatch('[a-f0-9]{32}', attempt_id):
            raise WorkloadError('invalid attempt identity')
        return self.prefix + attempt_id + '.service'

    def command(self, *args, check=True):
        return subprocess.run(args, check=check, capture_output=True, text=True, timeout=30)

    def inspect(self, attempt_id):
        reply = self.command('systemctl', '--user', 'show', self.unit(attempt_id),
                             '--property=LoadState,ActiveState,SubState,Result,ControlGroup')
        return dict(line.split('=', 1) for line in reply.stdout.splitlines() if '=' in line)

    def start(self, attempt_id, argv, cwd, output, *, env, cpu, memory_bytes,
              max_seconds=3600, tasks=512, log_bytes=8*1024**2, lease_file=None, rootless_docker=False):
        # Limits are operator/handler configuration, never unconstrained argv
        # supplied by a remote caller. Fail closed when cgroups cannot apply.
        for value in (cpu, memory_bytes, max_seconds, tasks, log_bytes):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise WorkloadError('execution limits must be positive and finite')
        if not isinstance(tasks, int) or not 1 <= tasks <= 8192:
            raise WorkloadError('task limit must be an integer from 1 to 8192')
        if not argv or not Path(argv[0]).is_absolute():
            raise WorkloadError('installed handler must name an absolute executable')
        if self.inspect(attempt_id).get('LoadState') != 'not-found':
            raise WorkloadError('attempt already has a unit; reconcile before launch', 409)
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        if rootless_docker:
            argv = docker_runtime.prepare(self.unit(attempt_id), argv, Path(cwd).resolve(), output)
        config = output/'execution.json'
        config.write_text(encode(dict(argv=argv, cwd=str(Path(cwd).resolve()), output=str(output),
                                      env=env, log_bytes=int(log_bytes),
                                      lease_file=str(lease_file) if lease_file else None)))
        config.chmod(0o600)
        wrapper = Path(__file__).with_name('bounded_exec.py').resolve()
        self.command('systemd-run', '--user', '--quiet', '--unit='+self.unit(attempt_id),
            '--property=Type=exec',
            '--property=KillMode=control-group', '--property=TimeoutStopSec=5s',
            '--property=SendSIGKILL=yes', '--property=OOMPolicy=kill',
            '--property=MemoryMax='+str(int(memory_bytes)), '--property=MemorySwapMax=0',
            '--property=CPUQuota='+str(cpu*100)+'%', '--property=TasksMax='+str(int(tasks)),
            '--property=RuntimeMaxSec='+str(max_seconds),
            '--property=StandardOutput=null', '--property=StandardError=null',
            '--property=NoNewPrivileges='+('no' if rootless_docker else 'yes'),
            *(['--property=Delegate=yes', '--property=DelegateSubgroup=supervisor'] if rootless_docker else []),
            sys.executable, str(wrapper), str(config))

    def stop(self, attempt_id):
        """Return only after the owned unit and all its descendants are gone."""
        state = self.inspect(attempt_id)
        if state.get('LoadState') == 'not-found':
            docker_runtime.cleanup(self.unit(attempt_id))
            return
        group = state.get('ControlGroup')
        stopped = self.command('systemctl', '--user', 'stop', self.unit(attempt_id), check=False)
        # A transient unit can be collected after inspect and before stop.
        # Exit 5 alone proves nothing: still verify unit state and the captured
        # cgroup below before acknowledging cleanup or releasing capacity.
        if stopped.returncode not in (0, 5):
            stopped.check_returncode()
        state = self.inspect(attempt_id)
        if state.get('ActiveState') not in ('inactive', 'failed') and state.get('LoadState') != 'not-found':
            raise WorkloadError('owned unit has not stopped; capacity remains reserved', 503)
        if group:
            events = Path('/sys/fs/cgroup')/group.lstrip('/')/'cgroup.events'
            if events.exists() and 'populated 1' in events.read_text():
                raise WorkloadError('owned cgroup still populated; capacity remains reserved', 503)
        self.command('systemctl', '--user', 'reset-failed', self.unit(attempt_id), check=False)
        docker_runtime.cleanup(self.unit(attempt_id))

    def exit_result(self, output):
        path = Path(output)/'exit.json'
        if not path.exists():
            return None
        if path.stat().st_size > 1024:
            raise WorkloadError('oversized execution result')
        return json.loads(path.read_text())
