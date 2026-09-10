"""Persistent one-slot Linux/WSL worker, independent of submitting clients.

Handlers are installed argv vectors. Production workspaces must be a dedicated
bounded filesystem. Rootless Docker handlers use a private daemon with containers
parented inside the same delegated attempt cgroup.
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
from pathlib import Path
import shutil
import time
import uuid
from urllib.error import HTTPError

from .archive import relative_path, unpack
from .docker_runtime import remove_data
from .client import WorkloadClient
from .lease import LeaseKeeper
from .model import WorkloadError, encode
from .supervision import SystemdExecutor, WorkerJournal
from .transfer import InputTransfer


class WorkloadWorker:
    def __init__(self, config):
        self.config = config
        self.client = WorkloadClient(config['authority'], config['token'])
        self.journal = WorkerJournal(config['state_dir'])
        self.executor = SystemdExecutor(config['worker'])
        self.boot = uuid.uuid4().hex
        self.workspace = Path(config['workspace']).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.handlers = config['handlers']
        if not self.handlers or any(h.get('backend', 'native') not in ('native', 'rootless-docker') for h in self.handlers.values()):
            raise WorkloadError('worker requires installed native or rootless-docker handlers')
        transfer_timeout = config.get('transfer_timeout', self.client.timeout)
        if (isinstance(transfer_timeout, bool) or not isinstance(transfer_timeout, (int, float)) or
                not self.client.timeout <= transfer_timeout <= 3600):
            raise WorkloadError('transfer timeout must be between control timeout and one hour')
        # Bulk object PUT/GET may cross regions or wait for a configured mirror.
        # Keep that budget separate so control requests still fail fast.
        transfer_client = WorkloadClient(config['authority'], config['token'], timeout=transfer_timeout)
        self.transfer = InputTransfer(transfer_client)
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

    def report(self):
        capacity = dict(self.config['capacity'])
        stats = os.statvfs(self.workspace)
        filesystem_bytes = stats.f_blocks*stats.f_frsize
        if self.config.get('require_dedicated_filesystem', True):
            if self.workspace.stat().st_dev == self.workspace.parent.stat().st_dev:
                raise WorkloadError('workspace is not a dedicated bounded filesystem', 503)
            if filesystem_bytes > self.config['workspace_bytes']:
                raise WorkloadError('workspace filesystem exceeds configured byte bound', 503)
        memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        free_memory = int(memory['MemAvailable'].strip().split()[0])*1024
        available = dict(capacity)
        available['memory_bytes'] = max(0, min(capacity['memory_bytes'],
            free_memory-self.config.get('memory_reserve_bytes', 1024**3)))
        available['disk_bytes'] = max(0, min(capacity['disk_bytes'],
            stats.f_bavail*stats.f_frsize-self.config.get('disk_reserve_bytes', 1024**3)))
        for path in self.config.get('backing_filesystems', []):
            backing = os.statvfs(path)
            headroom = backing.f_bavail*backing.f_frsize-self.config.get('backing_reserve_bytes', 20*1024**3)
            available['disk_bytes'] = max(0, min(available['disk_bytes'], headroom))
        available['cpu'] = max(0, min(capacity['cpu'], (os.cpu_count() or 1)-os.getloadavg()[0]))
        return dict(capacity=capacity, available=available, labels=self.config.get('labels', {}),
                    handlers=list(self.handlers), ready=not self.config.get('observe_only', False))

    def register(self, cleaned=()):
        return self.client.request('worker/report', dict(boot=self.boot, report=self.report(), cleaned=list(cleaned)))

    def reconcile(self):
        old = self.journal.read()
        if old:
            self.executor.stop(old['assignment']['attempt_id'])
            completion = old.get('completion')
            if completion is None and old.get('phase') == 'running':
                output = self.workspace/old['assignment']['attempt_id']/'output'
                result = self.executor.exit_result(output)
                if result is not None:
                    try:
                        completion = self._completion_from_exit(old['assignment'], result)
                        completion = self._attach_artifacts(old['assignment'], completion, output)
                        self.journal.write(dict(assignment=old['assignment'], phase='completed', completion=completion))
                    except (WorkloadError, HTTPError) as error:
                        # Cancellation or immutable-deadline expiry can fence the
                        # attempt before a restarted worker uploads its recovered
                        # receipt. The authority already owns the terminal state;
                        # abandon these obsolete artifacts so cleanup can finish.
                        if error.status != 409:
                            raise
                        completion = None
            if completion:
                try:
                    self.client.request('worker/complete', completion)
                except WorkloadError as error:
                    if error.status != 409:
                        raise
            # A restart or interrupted execution is a new worker session. This
            # fences any old live assignment before cleanup is acknowledged.
            self.boot = uuid.uuid4().hex
        response = self.register()
        cleaned = []
        # The authority may have committed an assignment whose poll response
        # never reached us. Deterministic unit identity makes that recoverable.
        for attempt in response['cleanup']:
            self.executor.stop(attempt)
            path = self.workspace/attempt
            if path.exists():
                remove_data(path)
                shutil.rmtree(path)
            cleaned.append(attempt)
        if cleaned:
            response = self.register(cleaned)
        if not response['ready'] and not self.config.get('observe_only', False):
            raise WorkloadError('worker cleanup is incomplete', 503)
        if old:
            path = self.workspace/old['assignment']['attempt_id']
            if path.exists():
                remove_data(path)
                shutil.rmtree(path)
        self.journal.clear()
        self.reconciled = True

    def _completion_from_exit(self, assignment, result):
        handler = self.handlers[assignment['spec']['handler']]
        code = result['exit_code']
        resources = result.get('resources', {})
        resource_failure = resources.get('oom_kill', 0) > 0 or resources.get('pids_max_events', 0) > 0
        outcome = ('succeeded' if code == 0 else
            'infrastructure' if resource_failure or code in handler.get('infrastructure_exit_codes', []) or
            (handler.get('backend') == 'rootless-docker' and code == 75) else 'product_failure')
        return dict(outcome=outcome, result=result)

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
            artifact = self.transfer.put(path, assignment=assignment)
            if self.output_mirror:
                try:
                    self.output_mirror.put(artifact['digest'], path, self.transfer.max_bytes)
                except WorkloadError as error:
                    # The authority CAS remains canonical and downstream
                    # workers retain their authenticated fallback path.
                    logging.warning('artifact mirror unavailable for %s: %s', artifact['digest'], error)
            artifacts.append(dict(name=item, **artifact))
        completion['result']['artifacts'] = artifacts
        completion.update(boot=assignment['boot'], attempt_id=assignment['attempt_id'], fence=assignment['fence'],
                          input_digest=assignment['spec']['input_digest'])
        return completion

    def step(self):
        if self.journal.read():
            self.reconciled = False
        if not self.reconciled:
            self.reconcile()
        if self.input_cache:
            self.input_cache.prune()
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

    def execute(self, assignment):
        attempt = assignment['attempt_id']
        self.executor.unit(attempt)  # Validate before using identity as a path.
        root = self.workspace/attempt
        self.journal.write(dict(assignment=assignment, phase='preparing'))
        root.mkdir(exist_ok=False)
        lease = None
        completion = None
        output = root/'output'
        try:
            lease = LeaseKeeper(self.client, assignment, root/'lease',
                                interval=self.config.get('lease_interval', 10)).start()
            spec = assignment['spec']
            handler = self.handlers[spec['handler']]
            need = spec['need']
            if need.get('cpu', 0) <= 0 or need.get('memory_bytes', 0) < 64*1024**2:
                raise WorkloadError('native execution requires CPU and at least 64 MiB RAM')
            bundle = (self.input_cache.get(assignment) if self.input_cache else
                      self.transfer.get(spec['input_digest'], root/'input.tar', assignment=assignment))
            unpack(bundle, root/'source', spec['input_digest'])
            if not self.input_cache:
                bundle.unlink()
            objects = root/'input-objects'
            objects.mkdir()
            for item in spec.get('input_objects', []):
                destination = objects/item['name']
                received = self.transfer.get(item['digest'], destination, assignment=assignment)
                if received.stat().st_size != item['size']:
                    raise WorkloadError('input object size mismatch', 409)
            output.mkdir()
            (root/'request.json').write_text(encode(spec['payload']))
            env = dict(self.config.get('environment', {}))
            env.update(HOME=str(root/'home'), TMPDIR=str(root/'tmp'),
                       HARMONY_INPUT=str(root/'source'), HARMONY_OUTPUT=str(output),
                       HARMONY_INPUT_OBJECTS=str(objects),
                       HARMONY_REQUEST=str(root/'request.json'), HARMONY_ATTEMPT=attempt)
            for path in ('home', 'tmp'):
                (root/path).mkdir()
            if lease.lost.is_set():
                raise WorkloadError('execution lease lost during preparation', 409)
            self.journal.write(dict(assignment=assignment, phase='running'))
            self.executor.start(attempt, handler['argv'], root/'source', output, env=env,
                cpu=need['cpu'], memory_bytes=need['memory_bytes'],
                max_seconds=handler.get('max_seconds', 3600), tasks=handler.get('max_tasks', 512), lease_file=root/'lease',
                rootless_docker=handler.get('backend') == 'rootless-docker')
            # Once execution starts, worker-process health alone cannot retain
            # the slot. A live supervised unit or its durable exit receipt must
            # prove that execution still exists or has reached result handoff.
            lease.require_liveness(lambda: self._execution_live_or_complete(attempt, output))
            last_report = time.monotonic()
            while True:
                if lease.lost.is_set():
                    raise WorkloadError('execution lease lost', 409)
                result = self.executor.exit_result(output)
                if result is not None:
                    completion = self._completion_from_exit(assignment, result)
                    break
                if time.monotonic()-last_report >= 10:
                    self.register()
                    last_report = time.monotonic()
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
            logging.warning('attempt %s stopped: %s', attempt, type(error).__name__)
            completion = dict(outcome='infrastructure', result={'error':type(error).__name__})
            if isinstance(error, WorkloadError):
                completion['result']['detail'] = str(error)[:512]
        finally:
            # Never acknowledge completion or cleanup while owned work survives.
            try:
                self.executor.stop(attempt)
                remove_data(root)
            except Exception:
                if lease:
                    lease.close()
                raise
        try:
            completion = self._attach_artifacts(assignment, completion, output)
            self.journal.write(dict(assignment=assignment, phase='completed', completion=completion))
            self.client.request('worker/complete', completion)
        except (WorkloadError, HTTPError) as error:
            if error.status != 409:
                raise
            self.reconciled = False
        finally:
            if lease:
                lease.close()
        # Keep evidence until authority acknowledgement; failed network writes
        # leave the journal and root for the next reconciliation pass.
        shutil.rmtree(root)
        self.journal.clear()

    def close(self):
        self.journal.close()
