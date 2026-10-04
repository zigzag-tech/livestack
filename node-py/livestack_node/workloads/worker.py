"""Persistent one-slot Linux/WSL, macOS or Windows worker, independent of submitting clients.

Handlers are installed argv vectors. Production workspaces must be a dedicated
bounded filesystem. Rootless Docker handlers use a private daemon with containers
parented inside the same delegated attempt cgroup.
"""
from __future__ import annotations

import json
import hashlib
import logging
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid
from urllib.error import HTTPError

from ..hostview import HostView, cgroup_nonreclaimable, user_app_slice
from .archive import relative_path, unpack
from .docker_runtime import RuntimeCleanupRefused, remove_data
from .client import WorkloadClient
from .lease import LeaseKeeper, retry_transient, transient
from .model import WorkloadError, encode, name
from .supervision import SystemdExecutor, WorkerJournal
if sys.platform == 'darwin':
    from . import darwin_proc
    from .darwin_supervision import LaunchdExecutor
if sys.platform == 'win32':
    from . import windows_proc
    from .windows_supervision import JobObjectExecutor
from .transfer import InputTransfer


def rmtree_writable(path):
    """rmtree that also removes read-only trees.

    Handlers unpack read-only source trees (files or whole directories at 0555);
    unlinking needs write permission on the PARENT directory, so a plain rmtree
    raises PermissionError on them. On 2026-09-29 that escaped reconcile() and
    wedged a worker for hours. Make every real directory owner-writable, retry.
    """
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        pass
    for base, dirs, files in os.walk(path):
        # Windows refuses to unlink a FILE with the read-only attribute
        # (mode 0444 there), whatever its directory allows.
        names = [*dirs, *files] if os.name == 'nt' else dirs
        for name in [base, *(os.path.join(base, d) for d in names)]:
            if not os.path.islink(name):
                try:
                    os.chmod(name, 0o700 | os.lstat(name).st_mode)
                except OSError:
                    pass
    shutil.rmtree(path)


def _tree_bytes(path):
    total = 0
    for base, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


class WorkloadWorker:
    # Match the authority's object bound without widening input/downloads.
    MAX_HANDLER_OUTPUT_BYTES = 8 * 1024**3

    def __init__(self, config):
        self.config = config
        self.client = WorkloadClient(config['authority'], config['token'], edge_key=config.get('edge_key'))
        self.journal = WorkerJournal(config['state_dir'])
        # macOS has no cgroups: launchd jobs plus wrapper-enforced limits
        # (openspec/changes/apple-host-compilation).
        self.darwin = sys.platform == 'darwin'
        # Windows: one Job Object per attempt (openspec/changes/windows-host-worker).
        self.windows = sys.platform == 'win32'
        if self.darwin:
            self.executor = LaunchdExecutor(config['worker'], state_dir=config.get('launchd_state_dir'))
        else:
            self.executor = (JobObjectExecutor if self.windows else SystemdExecutor)(config['worker'])
        self._cpu_load = windows_proc.CpuLoad() if self.windows else None
        if self.darwin and config.get('host_pressure') is None and not config.get('remote_capacity_authoritative'):
            # The only memory reading a macOS worker has; without it absence of
            # a reading would look like absence of pressure.
            raise WorkloadError('a macOS worker requires a host_pressure reading')
        self.boot = name(config['boot'], 'worker boot') if config.get('boot') is not None else uuid.uuid4().hex
        self.workspace = Path(config['workspace']).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.handlers = config['handlers']
        self.task_environments = None
        self.task_environment_error = None
        self._environment_sweep_at = 0.0
        if config.get('task_environments') is not None:
            try:
                from .task_environments import TaskEnvironmentStore
                self.task_environments = TaskEnvironmentStore(config['task_environments'],
                    workspace=self.workspace, handlers=self.handlers)
            except Exception as error:
                self.task_environment_error = f'{type(error).__name__}: {str(error)[:512]}'
                logging.error('task_environment_support_disabled: %s', self.task_environment_error)
        if not self.handlers or any(h.get('backend', 'native') not in (
                'native', 'rootless-docker', 'rootless-docker-native') for h in self.handlers.values()):
            raise WorkloadError('worker requires installed native or rootless-docker handlers')
        transfer_timeout = config.get('transfer_timeout', self.client.timeout)
        if (isinstance(transfer_timeout, bool) or not isinstance(transfer_timeout, (int, float)) or
                not self.client.timeout <= transfer_timeout <= 3600):
            raise WorkloadError('transfer timeout must be between control timeout and one hour')
        # Bulk object PUT/GET may cross regions or wait for a configured mirror.
        # Keep that budget separate so control requests still fail fast.
        transfer_client = WorkloadClient(config['authority'], config['token'], timeout=transfer_timeout,
                                         edge_key=config.get('edge_key'))
        spec = config.get('object_relay')
        if spec is not None and (not isinstance(spec, dict) or not {'url', 'key'} <= set(spec) <= {'url', 'key', 'parallel'}
                                 or not isinstance(spec.get('parallel', 4), int) or isinstance(spec.get('parallel', 4), bool)
                                 or not 1 <= spec.get('parallel', 4) <= 8):
            raise WorkloadError('object_relay requires url and key, and optionally parallel 1..8')
        transfer_options = {} if spec is None else dict(
            relay=WorkloadClient(spec['url'], config['token'], timeout=transfer_timeout,
                                 edge_key=config.get('edge_key')),
            relay_key=spec['key'], relay_parallel=spec.get('parallel', 4))
        self.transfer = InputTransfer(transfer_client, **transfer_options)
        # Large compiler artifacts use their handler's output bound; input
        # downloads and framework logs keep the original transfer limit.
        self.output_transfers = {}
        for handler_name, handler in self.handlers.items():
            output_max_bytes = handler.get('output_max_bytes', self.transfer.max_bytes)
            if (isinstance(output_max_bytes, bool) or not isinstance(output_max_bytes, int) or
                    not 1 <= output_max_bytes <= self.MAX_HANDLER_OUTPUT_BYTES):
                raise WorkloadError('handler output_max_bytes must be an integer from 1 byte to 8 GiB')
            self.output_transfers[handler_name] = (
                self.transfer if output_max_bytes == self.transfer.max_bytes else
                InputTransfer(transfer_client, max_bytes=output_max_bytes, **transfer_options))
        self.output_mirror = None
        if config.get('output_mirror') is not None:
            from .artifact_mirror import InstalledArtifactMirror
            self.output_mirror = InstalledArtifactMirror(config['output_mirror'])
        mirror = None
        if config.get('input_mirror') is not None:
            from .input_mirror import InstalledInputMirror
            mirror = InstalledInputMirror(config['input_mirror'])
        self.input_cache = None
        if config.get('input_cache_bytes', 0):
            from .input_cache import InputCache
            cache_name = 'input-cache-'+hashlib.sha256(config['worker'].encode()).hexdigest()[:16]
            self.input_cache = InputCache(self.workspace/cache_name, self.transfer,
                max_bytes=config['input_cache_bytes'], max_entries=config.get('input_cache_entries', 32),
                retention_seconds=config.get('input_cache_retention_seconds', 14*86400), mirror=mirror)
        self.reconciled = False
        self._host_pressure_state = None
        # The measured host every placement on it consults
        # (openspec/changes/host-memory-ledger). Model servers listed in
        # `host_services` are charged their learned host-RAM peak.
        self.host_view = HostView(services=config.get('host_services', []),
                                  peaks_path=Path(config['state_dir'])/'host-peaks-nonreclaimable.json',
                                  reserve_bytes=config.get('memory_reserve_bytes', 1024**3))
        # Workspaces that could not be removed: path -> last error text. They are
        # retried every step; a removal failure never blocks claiming/heartbeats.
        self.stuck_workspaces = {}
        self._logged_cleanup_failures = set()
        # Attempts whose unit is gone but whose Docker runtime dir could not be
        # removed: retried each step, never blocking claiming.
        self.stuck_runtimes = set()

    def _stop(self, attempt):
        """executor.stop, except that a refused runtime-dir removal is not fatal.

        stop() raises RuntimeCleanupRefused only AFTER the unit and cgroup are
        verified gone (capacity is safe to release). One named log line per
        distinct (attempt, cause); retried by _retry_stuck_workspaces.
        """
        try:
            self.executor.stop(attempt)
            self.stuck_runtimes.discard(attempt)
        except RuntimeCleanupRefused as error:
            self.stuck_runtimes.add(attempt)
            key = ('runtime', attempt, str(error))
            if key not in self._logged_cleanup_failures:
                self._logged_cleanup_failures.add(key)
                logging.warning('docker runtime cleanup failed for attempt %s: %s (%d stuck runtimes)',
                                attempt, error, len(self.stuck_runtimes))

    def _remove_workspace(self, path):
        """Remove an attempt workspace; on failure keep it for the next turn.

        Logs ONE named line per distinct (path, error), so a permanent failure
        cannot spam the rotating log; the line carries the running count/bytes.
        """
        path = Path(path)
        try:
            if path.exists():
                remove_data(path)
                rmtree_writable(path)
            self.stuck_workspaces.pop(str(path), None)
            return True
        except Exception as error:
            text = '%s: %s' % (type(error).__name__, error)
            self.stuck_workspaces[str(path)] = text
            if (str(path), text) not in self._logged_cleanup_failures:
                self._logged_cleanup_failures.add((str(path), text))
                logging.warning('workspace cleanup failed: %s: %s (%d un-removable workspaces, %d bytes)',
                    path, text, len(self.stuck_workspaces),
                    sum(_tree_bytes(p) for p in self.stuck_workspaces))
            return False

    def _retry_stuck_workspaces(self):
        for attempt in list(self.stuck_runtimes):
            self._stop(attempt)
        for path in list(self.stuck_workspaces):
            self._remove_workspace(path)

    def _host_memory_limit(self):
        """Memory the HOST says it can spare, or None when no host is configured.

        A worker inside a VM sees only the guest's /proc/meminfo, which stays
        healthy while the host swaps: on 2026-09-28 a Lima guest reported ~15 GB
        available while a leaking emulator held 32 GB of its 36 GB macOS host, the
        guest missed lease renewals, and every attempt ended as an unexplained
        infrastructure failure. A host-side publisher writes
        {"ts": <epoch s>, "available_memory_bytes": <int>} to a file the guest can
        read; a missing, stale or malformed file reports 0, never "no limit" —
        absence of the reading must not look like absence of pressure.
        """
        spec = self.config.get('host_pressure')
        if spec is None:
            return None
        limit, state = 0, 'unreadable'
        try:
            reading = json.loads(Path(spec['path']).read_text())
            available, stamp = reading['available_memory_bytes'], reading['ts']
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (available, stamp)) or available < 0:
                state = 'malformed'
            elif time.time()-stamp > spec.get('max_age_seconds', 120):
                state = 'stale'
            else:
                limit, state = int(available), 'ok'
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if state != self._host_pressure_state:
            (logging.info if state == 'ok' else logging.warning)(
                'host pressure reading %s (reporting %d bytes available from it)', state, limit)
            self._host_pressure_state = state
        return limit

    def _disk(self, path):
        """(filesystem bytes, bytes free to this user)."""
        if self.windows:
            usage = shutil.disk_usage(path)
            return usage.total, usage.free
        stats = os.statvfs(path)
        return stats.f_blocks*stats.f_frsize, stats.f_bavail*stats.f_frsize

    def _busy_cpus(self):
        if not self.windows:
            return os.getloadavg()[0]
        busy = self._cpu_load.sample(os.cpu_count() or 1)
        # Unknown (first sample) is charged as a fully busy host, never as idle.
        return (os.cpu_count() or 1) if busy is None else busy

    def report(self):
        filesystem_bytes, filesystem_free = self._disk(self.workspace)
        if self.windows:
            # Not the Linux `host` block either: a WSL VM worker on the same
            # physical host reports its own guest view under the same `host`
            # principal, and placement subtracts every attempt's admission on
            # that host from both. Windows' available memory already counts the
            # VM (vmmem) as a consumer.
            memory = windows_proc.memory_status()
            host = dict(memory_total_bytes=memory['total'], memory_available_bytes=memory['available'],
                        memory_reserve_bytes=self.host_view.reserve_bytes)
        elif self.darwin:
            # Not the Linux `host` block: placement's measured-host path keys by
            # physical host, and a Lima VM worker on this Mac must not have its
            # claims charged against macOS memory. Available memory comes from
            # the host_pressure clamp below.
            total = darwin_proc.memory_total()
            host = dict(memory_total_bytes=total, memory_available_bytes=total,
                        memory_reserve_bytes=self.host_view.reserve_bytes)
        else:
            host = self.host_view.sample()
        # `capacity` is an operator ceiling, not the description of the host.
        # Absent, the worker offers the measured machine and placement decides
        # fit from measurement and claims.
        capacity = dict(self.config.get('capacity') or dict(
            cpu=os.cpu_count() or 1, memory_bytes=host['memory_total_bytes'], disk_bytes=filesystem_bytes))
        if self.config.get('require_dedicated_filesystem', True):
            if self.workspace.stat().st_dev == self.workspace.parent.stat().st_dev:
                raise WorkloadError('workspace is not a dedicated bounded filesystem', 503)
            if filesystem_bytes > self.config['workspace_bytes']:
                raise WorkloadError('workspace filesystem exceeds configured byte bound', 503)
        available = dict(capacity)
        available['memory_bytes'] = max(0, min(capacity['memory_bytes'],
            host['memory_available_bytes']-host['memory_reserve_bytes']))
        host_limit = self._host_memory_limit()
        if host_limit is not None:
            available['memory_bytes'] = min(available['memory_bytes'], host_limit)
            # Inside a VM the guest's MemAvailable is not the machine's.
            host['memory_available_bytes'] = min(host['memory_available_bytes'], host_limit)
        available['disk_bytes'] = max(0, min(capacity['disk_bytes'],
            filesystem_free-self.config.get('disk_reserve_bytes', 1024**3)))
        for path in self.config.get('backing_filesystems', []):
            headroom = self._disk(path)[1]-self.config.get('backing_reserve_bytes', 20*1024**3)
            available['disk_bytes'] = max(0, min(available['disk_bytes'], headroom))
        available['cpu'] = max(0, min(capacity['cpu'], (os.cpu_count() or 1)-self._busy_cpus()))
        report = dict(capacity=capacity, available=available, labels=self.config.get('labels', {}),
                      handlers=list(self.handlers), ready=not self.config.get('observe_only', False))
        if self.task_environments is not None:
            profiles, replicas = self.task_environments.report()
            report['environment_profiles'] = profiles
            if replicas is not None:
                report['environment_replicas'] = replicas
        elif self.config.get('task_environments') is not None:
            # Explicit absence keeps ordinary handlers available while making
            # task environments ineligible for placement on this worker.
            report['environment_profiles'] = {}
        if not self.darwin and not self.windows:
            report['host'] = host
        return report

    def register(self, cleaned=()):
        response = self.client.request('worker/report', dict(boot=self.boot, report=self.report(), cleaned=list(cleaned)))
        instructions = response.get('environment_cleanup', [])
        if not isinstance(instructions, list) or len(instructions) > 64:
            raise WorkloadError('authority returned invalid environment cleanup instructions', 502)
        if instructions and self.task_environments is None:
            raise WorkloadError('authority requested cleanup on a worker without task environment storage', 503)
        seen = set()
        for item in instructions:
            if (not isinstance(item, dict) or set(item) != {'handle', 'generation'} or
                    not isinstance(item['handle'], str) or not re.fullmatch(r'[a-f0-9]{32}', item['handle']) or
                    type(item['generation']) is not int or item['generation'] < 0 or item['handle'] in seen):
                raise WorkloadError('authority returned invalid environment cleanup identity', 502)
            seen.add(item['handle'])
            try:
                outcome = self.task_environments.remove_stale_replica(item['handle'], item['generation'])
            except Exception as error:
                logging.error('task_environment_stale_cleanup_failed: handle=%s generation=%s error=%s: %s',
                              item['handle'], item['generation'], type(error).__name__, str(error)[:512])
                continue
            logging.info('task_environment_stale_cleanup: handle=%s generation=%s outcome=%s',
                         item['handle'], item['generation'], outcome)
        return response

    def reconcile(self):
        old = self.journal.read()
        if old:
            assignment = old['assignment']
            self._stop(assignment['attempt_id'])
            completion = old.get('completion')
            if completion is None and old.get('phase') == 'running':
                output = self.workspace/assignment['attempt_id']/'output'
                self._release_fleet_leases(output)
                result = self.executor.exit_result(output)
                if result is not None:
                    try:
                        completion = self._completion_from_exit(assignment, result)
                        completion = self._attach_artifacts(assignment, completion, output)
                    except (WorkloadError, HTTPError) as error:
                        # Cancellation or immutable-deadline expiry can fence the
                        # attempt before a restarted worker uploads its recovered
                        # receipt. The authority already owns the terminal state;
                        # abandon these obsolete artifacts so cleanup can finish.
                        if error.status != 409:
                            raise
                        completion = None
            env = assignment.get('environment')
            if env and completion is not None and 'environment_receipt' not in completion:
                completion['environment_receipt'] = self._environment_receipt(assignment, None,
                    self.workspace/assignment['attempt_id']/'output', source_seconds=None,
                    cleanup_seconds=None, state='rebuild_required')
            if completion is not None:
                self.journal.write(dict(assignment=assignment, phase='completed', completion=completion))
            if completion:
                try:
                    acknowledgement = self.client.request('worker/complete', completion)
                except WorkloadError as error:
                    if error.status != 409:
                        raise
                    completion = None
                if completion and env and self.task_environments and \
                        completion.get('environment_receipt', {}).get('state') == 'parked':
                    if not self.task_environments.acknowledge_handle(env['handle'], env['generation'],
                                                                      acknowledgement.get('environment')):
                        logging.error('task_environment_restart_ack_missing: handle=%s generation=%s',
                                      env['handle'], env['generation'])
                elif completion and env and self.task_environments:
                    self.task_environments.invalidate(env['handle'])
            if env and completion is None and self.task_environments:
                self.task_environments.invalidate(env['handle'])
            # A restart or interrupted execution is a new worker session. This
            # fences any old live assignment before cleanup is acknowledged.
            self.boot = uuid.uuid4().hex
        response = self.register()
        cleaned = []
        # The authority may have committed an assignment whose poll response
        # never reached us. Deterministic unit identity makes that recoverable.
        for attempt in response['cleanup']:
            self._stop(attempt)
            path = self.workspace/attempt
            # The attempt is stopped; a workspace that resists removal is retried
            # each step, but must not keep the authority from readvertising us.
            self._remove_workspace(path)
            cleaned.append(attempt)
        if cleaned:
            response = self.register(cleaned)
        if not response['ready'] and not self.config.get('observe_only', False):
            raise WorkloadError('worker cleanup is incomplete', 503)
        if old:
            path = self.workspace/old['assignment']['attempt_id']
            self._remove_workspace(path)
        self.journal.clear()
        self.reconciled = True

    def _completion_from_exit(self, assignment, result):
        handler = self.handlers[assignment['spec']['handler']]
        code = result['exit_code']
        resources = result.get('resources', {})
        resource_failure = resources.get('oom_kill', 0) > 0 or resources.get('pids_max_events', 0) > 0
        outcome = ('succeeded' if code == 0 else
            'infrastructure' if resource_failure or code in handler.get('infrastructure_exit_codes', []) or
            (handler.get('backend') in ('rootless-docker', 'rootless-docker-native') and code == 75) else 'product_failure')
        return dict(outcome=outcome, result=result)

    @staticmethod
    def _measured_phase(seconds, reason='measurement_unavailable'):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0 or not math.isfinite(seconds):
            return dict(seconds=None, reason=reason)
        return dict(seconds=float(seconds))

    def _environment_timings(self, assignment, output, *, transfer_seconds, source_seconds,
                             execution_seconds, cleanup_seconds):
        environment = assignment['environment']
        queue_seconds = environment.get('queue_seconds')
        timings = {
            'queue': self._measured_phase(queue_seconds, 'authority_queue_timing_unavailable'),
            'transfer': self._measured_phase(transfer_seconds, 'input_transfer_not_completed'),
            'source_materialization': self._measured_phase(source_seconds, 'source_materialization_not_started'),
            'dependencies': dict(seconds=None, reason='handler_uninstrumented'),
            'compile': dict(seconds=None, reason='handler_uninstrumented'),
            'test': dict(seconds=None, reason='handler_uninstrumented'),
            'execution': self._measured_phase(execution_seconds, 'handler_execution_not_started'),
            'cleanup': self._measured_phase(cleanup_seconds, 'cleanup_not_observed'),
        }
        trace = Path(output) / 'environment-timings.json'
        try:
            if not trace.is_file() or trace.is_symlink() or trace.stat().st_size > 2048:
                raise ValueError('timing trace missing or oversized')
            value = json.loads(trace.read_bytes())
            if not isinstance(value, dict):
                raise ValueError('timing trace fields are invalid')
            if value.get('version') == 1 and set(value) == {
                    'version', 'dependencies_seconds', 'compile_seconds', 'test_seconds'}:
                # Keep task-E2E handlers installed before the phase-reason
                # format upgrade readable during a rolling worker deployment.
                phases = {phase: {'seconds': value[phase + '_seconds']}
                          for phase in ('dependencies', 'compile', 'test')}
            elif value.get('version') == 2 and set(value) == {'version', 'phases'}:
                phases = value['phases']
                if not isinstance(phases, dict) or set(phases) != {'dependencies', 'compile', 'test'}:
                    raise ValueError('timing trace phases are invalid')
            else:
                raise ValueError('timing trace fields are invalid')
            for phase, measurement in phases.items():
                if not isinstance(measurement, dict) or set(measurement) not in ({'seconds'}, {'seconds', 'reason'}):
                    raise ValueError(f'{phase} timing fields are invalid')
                seconds = measurement['seconds']
                reason = measurement.get('reason')
                if seconds is None:
                    if not isinstance(reason, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', reason):
                        raise ValueError(f'{phase} unknown timing needs a reason')
                    timings[phase] = self._measured_phase(None, reason)
                elif (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or
                        not 0 <= seconds <= 86400 or not math.isfinite(seconds) or reason is not None):
                    raise ValueError(f'{phase} timing is invalid')
                else:
                    timings[phase] = dict(seconds=float(seconds))
        except (OSError, ValueError, TypeError) as error:
            logging.info('task_environment_timing_unknown: job=%s reason=%s: %s',
                         assignment['job_id'], type(error).__name__, str(error)[:256])
        return timings

    def _environment_receipt(self, assignment, prepared, output, *, transfer_seconds=None, source_seconds,
                             execution_seconds=None, cleanup_seconds, state):
        if not assignment.get('environment'):
            return None
        timings = self._environment_timings(assignment, output,
            transfer_seconds=transfer_seconds, source_seconds=source_seconds,
            execution_seconds=execution_seconds, cleanup_seconds=cleanup_seconds)
        if prepared is not None and self.task_environments is not None:
            return self.task_environments.receipt(prepared, state=state, phase_timings=timings)
        environment = assignment['environment']
        compatibility = (self.task_environments.profile_digest(environment['profile'])
                         if self.task_environments else None) or environment.get('compatibility')
        if not isinstance(compatibility, str) or not re.fullmatch('[a-f0-9]{64}', compatibility):
            compatibility = hashlib.sha256(('unavailable:' + environment['profile']).encode()).hexdigest()
        return dict(version=1, handle=environment['handle'], generation=environment['generation'],
            profile=environment['profile'], compatibility=compatibility,
            source_digest=assignment['spec']['input_digest'], reuse_outcome='created',
            reason_code='worker_environment_unavailable',
            state='rebuild_required', bytes_used=0, phase_timings=timings, cache_components=[])

    def _close_lease(self, lease, attempt, in_flight):
        # A stuck renewal thread must never mask the error being propagated.
        try:
            lease.close()
        except Exception as error:
            logging.warning('lease close failed for %s: %s: %s', attempt, type(error).__name__, error)
            if in_flight is None:
                raise

    def _attach_artifacts(self, assignment, completion, output):
        handler = self.handlers[assignment['spec']['handler']]
        artifacts = []
        declared = handler.get('outputs', []) if completion['outcome'] != 'infrastructure' else handler.get('infrastructure_outputs', [])
        for item in ['command.log', 'command.previous.log'] + declared:
            relative_path(item)
            path = Path(output)/item
            if not path.exists():
                continue
            if path.resolve() != path or not path.is_file():
                raise WorkloadError('artifact must be a private regular file')
            output_transfer = (self.output_transfers[assignment['spec']['handler']]
                              if item in declared else self.transfer)
            artifact = output_transfer.put(path, assignment=assignment)
            if self.output_mirror:
                try:
                    self.output_mirror.put(artifact['digest'], path, output_transfer.max_bytes)
                except WorkloadError as error:
                    # The authority CAS remains canonical and downstream
                    # workers retain their authenticated fallback path.
                    logging.warning('artifact mirror unavailable for %s: %s', artifact['digest'], error)
            artifacts.append(dict(name=item, **artifact))
        if completion['outcome'] == 'infrastructure':
            # Run logs are the evidence of why execution died; ship them even
            # when the handler declared no outputs. Best effort per file: a log
            # that cannot upload must never block the result handoff, and it
            # rides the canonical path only (diagnostics, not program inputs).
            shipped = {artifact['name'] for artifact in artifacts}
            logs = sorted(Path(output).glob('*.log')) + sorted(Path(output).parent.glob('*.log'))
            for path in logs[:16]:
                if path.name in shipped:
                    continue
                shipped.add(path.name)
                try:
                    artifact = self.transfer.put(path, assignment=assignment)
                except Exception as error:
                    logging.warning('infrastructure log %s not shipped: %s: %s',
                                    path.name, type(error).__name__, error)
                    continue
                artifacts.append(dict(name=path.name, **artifact))
        completion['result']['artifacts'] = artifacts
        completion.update(boot=assignment['boot'], attempt_id=assignment['attempt_id'], fence=assignment['fence'],
                          input_digest=assignment['spec']['input_digest'])
        return completion

    def step(self):
        if self.journal.read():
            self.reconciled = False
        if not self.reconciled:
            self.reconcile()
        self._retry_stuck_workspaces()
        if self.input_cache:
            self.input_cache.prune()
        if self.task_environments and time.monotonic() >= self._environment_sweep_at:
            try:
                self.task_environments.prune()
            except Exception as error:
                logging.error('task_environment_sweep_failed: %s: %s', type(error).__name__, str(error)[:512])
            self._environment_sweep_at = time.monotonic() + 60
        response = self.register()
        if response['cleanup']:
            self.reconciled = False
            self.reconcile()
        if not response['ready']:
            return False
        assignment = self.client.request('worker/claim', {'boot':self.boot})['assignment']
        if assignment is None:
            return False
        self.execute(assignment)
        return True

    def _execution_live_or_complete(self, attempt, output):
        if self.executor.exit_result(output) is not None:
            return True
        if self.executor.alive(attempt):
            return True
        # Close the exit race: the wrapper atomically publishes its receipt
        # immediately before systemd marks the unit inactive.
        return self.executor.exit_result(output) is not None

    def _attempt_env(self, assignment, root, output, objects, attempt, source_path=None,
                     environment_prepared=None):
        """The handler's environment. Ownership metadata travels with the job:
        HARMONY_OWNER is the end user's owner string (labels.owner) when the
        submitter named one, else the submitting principal itself."""
        env = dict(self.config.get('environment', {}))
        spec = assignment['spec']
        owner = (spec.get('labels') or {}).get('owner') or assignment['owner']
        env.update(HOME=str(root/'home'), TMPDIR=str(root/'tmp'),
                   HARMONY_INPUT=str(source_path or root/'source'), HARMONY_OUTPUT=str(output),
                   HARMONY_INPUT_OBJECTS=str(objects),
                   HARMONY_REQUEST=str(root/'request.json'), HARMONY_ATTEMPT=attempt,
                   HARMONY_OWNER=owner)
        environment = assignment.get('environment')
        if environment is not None:
            env.update(HARMONY_ENV_HANDLE=environment['handle'],
                       HARMONY_ENV_GENERATION=str(environment['generation']),
                       HARMONY_ENV_PROFILE=environment['profile'],
                       HARMONY_PHASE_TIMINGS=str(output/'environment-timings.json'))
            components = [] if environment_prepared is None else [
                {key: component[key] for key in ('name', 'path', 'identity', 'outcome')}
                for component in environment_prepared['cache_components']]
            encoded_components = json.dumps(components, separators=(',', ':'))
            if len(encoded_components.encode()) > 16 * 1024:
                raise WorkloadError('task environment cache component handoff exceeds16KiB', 413)
            env['HARMONY_ENV_CACHE_COMPONENTS'] = encoded_components
        if self.windows:
            # Windows tools read TEMP/TMP, not TMPDIR. The job's name lets a
            # handler find its attempt job (IsProcessInJob proves membership).
            env.update(TEMP=str(root/'tmp'), TMP=str(root/'tmp'), HARMONY_JOB_OBJECT=self.executor.unit(attempt))
        compilation = assignment.get('compilation')
        if compilation is not None:
            if (type(self.config.get('compilation_launch_contract')) is not int or
                    self.config['compilation_launch_contract'] != 1 or
                    type(compilation.get('version')) is not int or compilation['version'] != 1):
                raise WorkloadError('compilation_launch_contract_unsupported', 403)
            env.update(HARMONY_WORKER=assignment['worker'], HARMONY_BOOT=assignment['boot'],
                       HARMONY_JOB=assignment['job_id'], HARMONY_FENCE=str(assignment['fence']),
                       HARMONY_INPUT_DIGEST=spec['input_digest'],
                       HARMONY_PHYSICAL_HOST=compilation['host'],
                       HARMONY_POLICY_REVISION=compilation['policy_revision'])
        if spec.get('execution_provider') is not None:
            # The signed credential returned from the OIDC bootstrap is scoped
            # to one remote workflow and one queued Harmony job. The compiler
            # guard revalidates the current attempt/fence through the authority
            # before each native compile launch.
            env.update(HARMONY_EXECUTION_PROVIDER=spec['execution_provider'],
                       HARMONY_AUTHORITY=self.config['authority'],
                       HARMONY_REMOTE_TOKEN=self.config['token'])
        if self.config.get('fleet_url'):
            env['HARMONY_FLEET_URL'] = self.config['fleet_url']
        if self.config.get('fleet_token'):
            env['HARMONY_FLEET_TOKEN'] = self.config['fleet_token']
        return env

    def _release_fleet_leases(self, output, *, status=None, wall_s=None):
        """Release fleet residency leases the handler recorded in
        $HARMONY_OUTPUT/leases.json, even after a crash. A dead fleet broker
        must not wedge attempt cleanup: leases expire by TTL, so failures are
        logged and swallowed. `status`/`wall_s` report how the workload went,
        when this process watched it run."""
        url = self.config.get('fleet_url')
        if not url:
            return
        from .lease_helper import release_leftovers
        try:
            released = release_leftovers(url, Path(output)/'leases.json',
                                         status=status, wall_s=wall_s)
        except Exception as error:
            logging.warning('fleet lease cleanup failed for %s: %s', output, error)
            return
        for lease_id in released:
            logging.info('released fleet lease %s from %s', lease_id, output)

    def execute(self, assignment):
        attempt = assignment['attempt_id']
        self.executor.unit(attempt)  # Validate before using identity as a path.
        root = self.workspace/attempt
        self.journal.write(dict(assignment=assignment, phase='preparing'))
        root.mkdir(exist_ok=False)
        lease = None
        completion = None
        started = None
        environment_prepared = None
        environment_view = None
        environment_isolation = {}
        source_materialization_started = None
        source_materialization_seconds = None
        transfer_seconds = 0.0
        execution_seconds = None
        cleanup_seconds = None
        output = root/'output'
        try:
            lease = LeaseKeeper(self.client, assignment, root/'lease',
                                interval=self.config.get('lease_interval', 10),
                                progress_path=output/'progress.json').start()
            spec = assignment['spec']
            handler = self.handlers[spec['handler']]
            need = spec['need']
            if need.get('cpu', 0) <= 0 or need.get('memory_bytes', 0) < 64*1024**2:
                raise WorkloadError('native execution requires CPU and at least 64 MiB RAM')
            transfer_started = time.monotonic()
            bundle = (self.input_cache.get(assignment) if self.input_cache else
                      self.transfer.get(spec['input_digest'], root/'input.tar', assignment=assignment))
            transfer_seconds += time.monotonic() - transfer_started
            source_materialization_started = time.monotonic()
            unpack(bundle, root/'source', spec['input_digest'])
            if not self.input_cache:
                bundle.unlink()
            execution_source = root/'source'
            if assignment.get('environment') is not None:
                if self.task_environments is None:
                    raise WorkloadError('assigned task environment is unavailable on this worker', 503)
                environment_prepared = self.task_environments.prepare(assignment, root/'source',
                                                                       handler=spec['handler'])
                execution_source = environment_prepared['source']
                environment_view = root/'environment-view'
                environment_view.mkdir(mode=0o700)
                environment_isolation = dict(inaccessible_paths=[str(self.task_environments.root)],
                    bind_paths=[(str(execution_source), str(environment_view))])
                execution_cwd = environment_view
            else:
                execution_cwd = execution_source
            source_materialization_seconds = time.monotonic() - source_materialization_started
            objects = root/'input-objects'
            objects.mkdir()
            for item in spec.get('input_objects', []):
                destination = objects/item['name']
                transfer_started = time.monotonic()
                received = self.transfer.get(item['digest'], destination, assignment=assignment)
                transfer_seconds += time.monotonic() - transfer_started
                if received.stat().st_size != item['size']:
                    raise WorkloadError('input object size mismatch', 409)
            output.mkdir()
            (root/'request.json').write_text(encode(spec['payload']))
            env = self._attempt_env(assignment, root, output, objects, attempt, source_path=execution_cwd,
                                    environment_prepared=environment_prepared)
            for path in ('home', 'tmp'):
                (root/path).mkdir()
            if lease.lost.is_set():
                raise WorkloadError('execution lease lost during preparation', 409)
            self.journal.write(dict(assignment=assignment, phase='running'))
            started = time.monotonic()
            self.executor.start(attempt, handler['argv'], execution_cwd, output, env=env,
                cpu=need['cpu'], memory_bytes=need['memory_bytes'],
                max_seconds=handler.get('max_seconds', 3600), tasks=handler.get('max_tasks', 512), lease_file=root/'lease',
                rootless_docker=handler.get('backend') in ('rootless-docker', 'rootless-docker-native'),
                rootless_native=handler.get('backend') == 'rootless-docker-native',
                native_host_address=self.config.get('docker_native_host_address'),
                **environment_isolation)
            # Once execution starts, worker-process health alone cannot retain
            # the slot. A live supervised unit or its durable exit receipt must
            # prove that execution still exists or has reached result handoff.
            lease.require_liveness(lambda: self._execution_live_or_complete(attempt, output))
            last_report = time.monotonic()
            # The attempt's non-reclaimable high-water (anon+shmem+kernel), sampled
            # each turn. memory_peak_bytes counts page cache the kernel reclaims
            # freely, so placement learns claims from this figure instead
            # (openspec/changes/host-memory-ledger). A spike shorter than one
            # turn (~0.2 s) can be missed.
            attempt_cgroup = None if self.darwin or self.windows else user_app_slice()/self.executor.unit(attempt)
            nonreclaimable_peak = None
            while True:
                if lease.lost.is_set():
                    raise WorkloadError('execution lease lost', 409)
                # On macOS the wrapper samples the tree's physical footprint
                # (already non-reclaimable) and reports its peak in the receipt.
                sample = (None if self.darwin else self.executor.memory_peak(attempt) if self.windows
                          else cgroup_nonreclaimable(attempt_cgroup))
                if sample is not None:
                    nonreclaimable_peak = max(nonreclaimable_peak or 0, sample)
                result = self.executor.exit_result(output)
                if result is not None:
                    if self.darwin:
                        nonreclaimable_peak = (result.get('resources') or {}).get('memory_peak_bytes')
                    if nonreclaimable_peak is not None:
                        result = dict(result, resources=dict(result.get('resources') or {},
                                      memory_nonreclaimable_peak_bytes=nonreclaimable_peak))
                    completion = self._completion_from_exit(assignment, result)
                    execution_seconds = time.monotonic() - started
                    break
                if time.monotonic()-last_report >= self.config.get('status_report_seconds', 10):
                    last_report = time.monotonic()
                    try:
                        self.register()
                    except Exception as error:
                        # A blip in reaching the authority must not kill a healthy
                        # attempt: the lease keeper owns the fence and stops us
                        # when the lease is really refused or expired.
                        if not transient(error):
                            raise
                        logging.warning('attempt %s: authority unreachable for status report (lease has %.0fs left): %s: %s',
                                        attempt, max(0, lease.deadline-time.monotonic()), type(error).__name__, error)
                state = self.executor.inspect(attempt)
                if state.get('ActiveState') in ('failed', 'inactive') or state.get('LoadState') == 'not-found':
                    # The wrapper can publish its receipt and exit between our
                    # first read and systemd inspection. Classify that receipt
                    # on the next iteration instead of retrying completed work.
                    if self.executor.exit_result(output) is not None:
                        continue
                    raise WorkloadError('execution stopped without a result', 503)
                time.sleep(.2)
        except Exception as error:
            if started is not None:
                execution_seconds = time.monotonic() - started
            detail = str(error)[:512] or type(error).__name__
            logging.warning('attempt %s stopped: %s: %s', attempt, type(error).__name__, detail)
            completion = dict(outcome='infrastructure', result={'error':type(error).__name__})
            completion['result']['detail'] = detail
        finally:
            # Never acknowledge completion or cleanup while owned work survives.
            cleanup_started = time.monotonic()
            try:
                self._stop(attempt)
                remove_data(root)
                cleanup_seconds = time.monotonic() - cleanup_started
            except Exception:
                if lease:
                    self._close_lease(lease, attempt, sys.exc_info()[0])
                if environment_prepared and self.task_environments:
                    self.task_environments.release(environment_prepared)
                raise
            if assignment.get('environment') is not None:
                parked = completion['outcome'] in ('succeeded', 'product_failure') and attempt not in self.stuck_runtimes
                if parked and environment_prepared is not None:
                    try:
                        self.task_environments.verify_source(environment_prepared)
                    except Exception as error:
                        parked = False
                        completion['outcome'] = 'infrastructure'
                        completion['result']['environment_integrity_error'] = str(error)[:512]
                        logging.error('task_environment_source_integrity_failed: job=%s: %s: %s',
                                      assignment['job_id'], type(error).__name__, error)
                try:
                    environment_receipt = self._environment_receipt(assignment, environment_prepared, output,
                        transfer_seconds=transfer_seconds, source_seconds=source_materialization_seconds,
                        execution_seconds=execution_seconds, cleanup_seconds=cleanup_seconds,
                        state='parked' if parked else 'rebuild_required')
                except Exception as error:
                    parked = False
                    logging.error('task_environment_receipt_failed: job=%s: %s: %s',
                                  assignment['job_id'], type(error).__name__, str(error)[:512])
                    environment_receipt = self._environment_receipt(assignment, None, output,
                        transfer_seconds=transfer_seconds, source_seconds=source_materialization_seconds,
                        execution_seconds=execution_seconds, cleanup_seconds=cleanup_seconds,
                        state='rebuild_required')
                completion['environment_receipt'] = environment_receipt
                if environment_prepared is not None and self.task_environments is not None:
                    try:
                        self.task_environments.mark_awaiting_authority(environment_prepared,
                            state=environment_receipt['state'], bytes_used=environment_receipt['bytes_used'])
                    except Exception as error:
                        logging.error('task_environment_local_handoff_failed: handle=%s: %s: %s',
                                      environment_prepared['handle'], type(error).__name__, str(error)[:512])
                        self.task_environments.reject(environment_prepared)
                        environment_receipt.update(state='rebuild_required', bytes_used=0, cache_components=[])
        # The workload's wall time and verdict go back with the fleet leases it
        # held: the fleet broker joins them to the decision that placed it.
        self._release_fleet_leases(
            output,
            status=None if started is None else
            'ok' if completion['outcome'] == 'succeeded' else 'failed',
            wall_s=None if started is None else time.monotonic() - started)
        try:
            # The upload is idempotent by digest and the lease keeper is still
            # renewing: ride out a short authority outage instead of dropping
            # the finished result. Exhaustion raises with the cause named.
            handoff = dict(budget=self.config.get('handoff_retry_seconds', 60),
                           keep_going=lambda: lease is None or not lease.lost.is_set())
            completion = retry_transient(lambda: self._attach_artifacts(assignment, completion, output),
                                         'attempt %s result upload' % attempt, **handoff)
            self.journal.write(dict(assignment=assignment, phase='completed', completion=completion))
            acknowledged = retry_transient(lambda: self.client.request('worker/complete', completion),
                            'attempt %s completion' % attempt, **handoff)
            if environment_prepared is not None and self.task_environments is not None and \
                    completion.get('environment_receipt', {}).get('state') == 'parked':
                try:
                    if not self.task_environments.acknowledge(environment_prepared, acknowledged.get('environment')):
                        logging.error('task_environment_authority_ack_missing: handle=%s generation=%s',
                                      environment_prepared['handle'], environment_prepared['generation'])
                except Exception as error:
                    logging.error('task_environment_local_park_failed: handle=%s: %s: %s',
                                  environment_prepared['handle'], type(error).__name__, str(error)[:512])
        except (WorkloadError, HTTPError) as error:
            if error.status != 409:
                raise
            if environment_prepared is not None and self.task_environments is not None:
                self.task_environments.reject(environment_prepared)
            self.reconciled = False
        finally:
            if lease:
                self._close_lease(lease, attempt, sys.exc_info()[0])
            if environment_prepared is not None and self.task_environments is not None:
                self.task_environments.release(environment_prepared)
        # Keep evidence until authority acknowledgement; failed network writes
        # leave the journal and root for the next reconciliation pass.
        # A workspace that resists removal is retried by later steps; the finished
        # attempt is already acknowledged and must not turn into a wedge.
        self._remove_workspace(root)
        self.journal.clear()

    def close(self):
        self.journal.close()
