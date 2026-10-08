"""Durable ownership and cgroup supervision for one Linux/WSL worker slot.

No PID-based recovery: systemd unit names are journaled before launch and
identify the entire cgroup. Rootless Docker jobs delegate a subtree and explicitly
parent every container beneath it; host Docker daemon work is never selected.
"""
from __future__ import annotations

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
if sys.platform == 'win32':
    import msvcrt
else:
    import fcntl


class WorkerJournal:
    """One fixed-size active record, protected by a process-lifetime lock."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.root/'worker.lock').open('a')
        try:
            if sys.platform == 'win32':
                # A byte-range lock held for the process lifetime; Windows
                # releases it when the process dies, as flock does.
                self.lock.seek(0)
                try:
                    msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:  # EDEADLOCK/EACCES: held by another process
                    raise BlockingIOError(str(error)) from error
            else:
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
        if sys.platform == 'win32':
            # No directory handles through os.open; NTFS journals the rename.
            return
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
        self.manager_attempt = None
        self.manager = None

    def unit(self, attempt_id):
        if not re.fullmatch('[a-f0-9]{32}', attempt_id):
            raise WorkloadError('invalid attempt identity')
        return self.prefix + attempt_id + '.service'

    def command(self, *args, check=True):
        return subprocess.run(args, check=check, capture_output=True, text=True, timeout=30)

    def _inspect_manager(self, manager, unit):
        reply = self.command('systemctl', manager, 'show', unit,
                             '--property=LoadState,ActiveState,SubState,Result,ControlGroup,OOMKills,MemoryPeak,CPUUsageNSec,ExecMainStatus')
        state = dict(line.split('=', 1) for line in reply.stdout.splitlines() if '=' in line)
        state['UnitManager'] = manager
        return state

    def inspect(self, attempt_id):
        manager = self.manager if self.manager_attempt == attempt_id else None
        if manager is not None:
            return self._inspect_manager(manager, self.unit(attempt_id))
        user = self._inspect_manager('--user', self.unit(attempt_id))
        system = self._inspect_manager('--system', self.unit(attempt_id))
        user_loaded = user.get('LoadState') != 'not-found'
        system_loaded = system.get('LoadState') != 'not-found'
        if user_loaded and system_loaded:
            raise WorkloadError('attempt unit exists in both systemd managers', 409)
        if system_loaded:
            if self.manager_attempt in (None, attempt_id):
                self.manager_attempt, self.manager = attempt_id, '--system'
            return system
        if user_loaded:
            if self.manager_attempt in (None, attempt_id):
                self.manager_attempt, self.manager = attempt_id, '--user'
            return user
        return user

    def alive(self, attempt_id):
        state = self.inspect(attempt_id)
        return (state.get('LoadState') == 'loaded' and
                state.get('ActiveState') in ('active', 'activating', 'reloading'))

    def start(self, attempt_id, argv, cwd, output, *, env, cpu, memory_bytes,
              max_seconds=3600, tasks=512, log_bytes=8*1024**2, lease_file=None, rootless_docker=False,
              rootless_native=False, native_host_address=None, docker_cache=None, inaccessible_paths=(), bind_paths=(),
              host_identity_required=False):
        # Limits are operator/handler configuration, never unconstrained argv
        # supplied by a remote caller. Fail closed when cgroups cannot apply.
        for value in (cpu, memory_bytes, max_seconds, tasks, log_bytes):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise WorkloadError('execution limits must be positive and finite')
        if not isinstance(tasks, int) or not 1 <= tasks <= 8192:
            raise WorkloadError('task limit must be an integer from 1 to 8192')
        if not argv or not Path(argv[0]).is_absolute():
            raise WorkloadError('installed handler must name an absolute executable')
        if rootless_native and not rootless_docker:
            raise WorkloadError('native Docker frontend requires owned rootless Docker')
        if type(host_identity_required) is not bool:
            raise WorkloadError('host identity requirement must be a boolean')
        if not isinstance(inaccessible_paths, (list, tuple)) or len(inaccessible_paths) > 8:
            raise WorkloadError('invalid execution inaccessible-path policy')
        clean_inaccessible = []
        for value in inaccessible_paths:
            path = Path(value)
            if (not path.is_absolute() or ':' in str(path) or '\n' in str(path) or
                    not path.exists() or path.is_symlink()):
                raise WorkloadError('execution inaccessible paths must be existing absolute paths')
            clean_inaccessible.append(path.resolve())
        if not isinstance(bind_paths, (list, tuple)) or len(bind_paths) > 8:
            raise WorkloadError('invalid execution bind-path policy')
        clean_binds = []
        for pair in bind_paths:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise WorkloadError('execution bind path must name source and destination')
            source, destination = (Path(value) for value in pair)
            if (not source.is_absolute() or not destination.is_absolute() or
                    any(':' in str(path) or '\n' in str(path) for path in (source, destination)) or
                    not source.is_dir() or source.is_symlink() or
                    not destination.is_dir() or destination.is_symlink()):
                raise WorkloadError('execution bind paths must be existing absolute directories')
            clean_source, clean_destination = source.resolve(), destination.resolve()
            if any(clean_destination == path or path in clean_destination.parents for path in clean_inaccessible):
                raise WorkloadError('execution bind destination is hidden by its path policy')
            clean_binds.append((clean_source, clean_destination))
        if self.inspect(attempt_id).get('LoadState') != 'not-found':
            raise WorkloadError('attempt already has a unit; reconcile before launch', 409)
        system_manager = bool(host_identity_required or
                              (rootless_docker and (clean_inaccessible or clean_binds)))
        manager = '--system' if system_manager else '--user'
        self.manager_attempt, self.manager = attempt_id, manager
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        if rootless_docker:
            argv = docker_runtime.prepare(self.unit(attempt_id), argv, Path(cwd).resolve(), output,
                                          native_client=rootless_native, native_host_address=native_host_address,
                                          docker_cache=docker_cache)
        config = output/'execution.json'
        config.write_text(encode(dict(argv=argv, cwd=str(Path(cwd).resolve()), output=str(output),
                                      env=env, log_bytes=int(log_bytes),
                                      lease_file=str(lease_file) if lease_file else None)))
        config.chmod(0o600)
        wrapper = Path(__file__).with_name('bounded_exec.py').resolve()
        run = (['/usr/bin/sudo', '-n', '/usr/bin/systemd-run', '--system'] if system_manager else
               ['systemd-run', '--user'])
        user_properties = (['--property=User='+str(os.getuid()), '--property=Group='+str(os.getgid())]
                           if system_manager else [])
        # In a --user manager PrivateTmp makes systemd build an implicit user namespace to
        # get a mount namespace; inside it setuid newuidmap (rootlesskit) fails with EPERM
        # because root is unmapped. So rootless Docker under --user gets no PrivateTmp.
        private_tmp = [] if rootless_docker and not system_manager else ['--property=PrivateTmp=yes']
        try:
            self.command(*run, '--quiet', '--unit='+self.unit(attempt_id),
                *user_properties,
                '--property=Type=exec',
                '--property=KillMode=control-group', '--property=TimeoutStopSec=5s',
                '--property=SendSIGKILL=yes', '--property=OOMPolicy=kill',
                '--property=MemoryMax='+str(int(memory_bytes)), '--property=MemorySwapMax=0',
                '--property=CPUQuota='+str(cpu*100)+'%', '--property=TasksMax='+str(int(tasks)),
                '--property=RuntimeMaxSec='+str(max_seconds),
                *private_tmp,
                '--property=StandardOutput=null', '--property=StandardError=null',
                '--property=NoNewPrivileges='+('no' if rootless_docker else 'yes'),
                *['--property=InaccessiblePaths='+str(path) for path in clean_inaccessible],
                *['--property=BindPaths='+str(source)+':'+str(destination)
                  for source, destination in clean_binds],
                *(['--property=Delegate=yes', '--property=DelegateSubgroup=supervisor'] if rootless_docker else []),
                sys.executable, str(wrapper), str(config))
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            if not host_identity_required:
                raise
            detail = (getattr(error, 'stderr', None) or getattr(error, 'stdout', None) or str(error)).strip()[-256:]
            raise WorkloadError('compilation_host_identity_unavailable: '+(detail or type(error).__name__), 503) from error

    def stop(self, attempt_id):
        """Return only after the owned unit and all its descendants are gone."""
        state = self.inspect(attempt_id)
        manager = state.get('UnitManager', '--user')
        system_command = ['/usr/bin/sudo', '-n', '/usr/bin/systemctl', '--system'] if manager == '--system' else [
            'systemctl', '--user']
        if state.get('LoadState') == 'not-found':
            docker_runtime.cleanup(self.unit(attempt_id))
            if self.manager_attempt == attempt_id:
                self.manager_attempt, self.manager = None, None
            return
        group = state.get('ControlGroup')
        stopped = self.command(*system_command, 'stop', self.unit(attempt_id), check=False)
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
        self.command(*system_command, 'reset-failed', self.unit(attempt_id), check=False)
        docker_runtime.cleanup(self.unit(attempt_id))
        if self.manager_attempt == attempt_id:
            self.manager_attempt, self.manager = None, None

    def exit_result(self, output):
        path = Path(output)/'exit.json'
        if not path.exists():
            return None
        if path.stat().st_size > 1024:
            raise WorkloadError('oversized execution result')
        return json.loads(path.read_text())
