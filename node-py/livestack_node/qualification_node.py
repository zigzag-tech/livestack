"""Qualification-only local model guard backed by real retained claims and leases.

The app enters work() INSIDE the physically tracked worker, after computing the
exact request digest. Exiting this scope does not retire host/model capacity.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Thread
import os
from pathlib import Path
import stat
import subprocess

from .provider_installation import sha
from .provider_owner_probe import control, process_start_ticks

from .lease import CapabilityLeaseStore
from .provider_qualification import QualificationClient
from .qualification_reservations import QualificationReservationReader


class QualificationNodeGate:
    def __init__(self, manager, client, reader, binding, *, owner_socket, unit_sha256):
        if not isinstance(client,QualificationClient) or not isinstance(reader,QualificationReservationReader) or not isinstance(manager.coordinator.broker,CapabilityLeaseStore):
            raise ValueError('actual_qualification_permission_claim_and_lease_components_required')
        if binding['identitySha256'] != client.source_digest or binding['configurationSha256'] != client.identity['configurationDigest'] or binding['unitKind'] not in manager.units or manager.coordinator.host_id != binding['hostId']:
            raise ValueError('qualification_node_source_kind_host_binding_required')
        self.manager,self.client,self.reader,self.binding = manager,client,reader,dict(binding)
        self.owner_socket,self.unit_sha256 = owner_socket,unit_sha256
        self._verify_instance()
        self.context = ContextVar('qualification-work',default=None)
        if getattr(manager,'_qualification_gate',None) is not None:
            raise ValueError('qualification_model_guard_already_installed')
        manager._qualification_gate = self
        original_ensure,original_load = manager.ensure,manager._load
        def ensure(name,device=None,budget=None):
            self._assert_work(name,device,budget)
            # Never let local policy evict another resident to make room.
            if set(manager._resident)-{name}:
                raise PermissionError('qualification_unrelated_local_residency_present')
            return original_ensure(name,device,budget)
        def load(name,device=None,budget=None):
            self._assert_work(name,device,budget)
            return original_load(name,device,budget)
        manager.ensure,manager._load = ensure,load

    def _verify_instance(self):
        _, configuration = self.client._verify()
        scope = configuration['intent']['scope']
        service = scope['service']
        if service not in ('polytts-qualification.service','polyasr-qualification.service') or configuration['intent']['targetMachine'] != self.binding['hostId'] or configuration['ssh']['expectedAuthority'] != self.binding['authority']:
            raise PermissionError('qualification_actual_node_service_scope_changed')
        pid = os.getpid()
        if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != self.binding['bootId'] or str(process_start_ticks(pid)) != self.binding['processStartTicks'] or '0::'+scope['controlGroup'] not in (Path('/proc')/str(pid)/'cgroup').read_text().splitlines():
            raise PermissionError('qualification_actual_node_process_changed')
        path = Path('/etc/systemd/system')/service
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode&0o022 or sha(path.read_bytes()) != self.unit_sha256:
            raise PermissionError('qualification_actual_node_unit_bytes_changed')
        observed = subprocess.run(['/usr/bin/systemctl','show',service,'--property=MainPID,InvocationID,NeedDaemonReload'],capture_output=True,text=True,check=True,timeout=20)
        if len(observed.stdout)>8192:raise PermissionError('qualification_actual_node_unit_observation_bound')
        values = dict(line.split('=',1) for line in observed.stdout.splitlines() if '=' in line)
        if values.get('MainPID') != str(pid) or values.get('InvocationID') != self.binding['unitInvocationId'] or values.get('NeedDaemonReload') != 'no':
            raise PermissionError('qualification_actual_node_unit_incarnation_changed')
        observed = control(self.owner_socket,{'operation':'status'})
        peer,result = observed['peer'],observed['receipt']['result']
        if peer['pid'] != pid or peer['presentAfterReply'] is not True or str(peer['startTicks']) != self.binding['processStartTicks'] or result['serverInstanceId'] != self.binding['serverInstanceId'] or result['sourceIdentity'] != self.client.identity:
            raise PermissionError('qualification_actual_local_owner_peer_changed')

    def _assert_work(self, name, device, budget):
        work = self.context.get()
        if work is None or name != self.binding['unitKind'] or device not in (None,self.binding['deviceId']) or budget is not None:
            raise PermissionError('qualification_exact_admitted_worker_context_required')
        job,fence,lease,failed = work
        if failed.is_set(): raise PermissionError('qualification_lease_heartbeat_failed')
        self.reader.assert_reserved(self.binding,job,fence)
        store = self.manager.coordinator.broker
        active = next((value for value in store.list_leases() if value.lease_id==lease.lease_id),None)
        if active is None or active.owner_id != job['ownerId'] or active.capability_kind != name or active.host_id != self.binding['hostId'] or active.expires_at <= store._now(None):
            raise PermissionError('qualification_actual_active_bound_kind_lease_required')

    @contextmanager
    def work(self, job, authorization, fence):
        if self.context.get() is not None:
            raise PermissionError('qualification_nested_worker_context_refused')
        self._verify_instance()
        self.reader.assert_reserved(self.binding,job,fence)
        self.client.authorize(effect='model-submit',job_id=job['jobId'],owner_id=job['ownerId'],input_digest=job['inputDigest'],model_kind=job['modelKind'],authorization=authorization)
        # acquire_lease itself does not ensure/load; normal facade /lease does.
        coordinator = self.manager.coordinator
        lease = coordinator.acquire_lease(self.binding['unitKind'],job['ownerId'],30)
        if lease is None:
            raise PermissionError('qualification_node_kind_capacity_unavailable')
        stop,failed = Event(),Event()
        def heartbeat():
            while not stop.wait(5):
                try:
                    updated = coordinator.heartbeat_lease(lease.lease_id,30)
                    if updated is None: raise PermissionError('qualification_lease_lost')
                except Exception:
                    failed.set();return
        thread = Thread(target=heartbeat,daemon=True,name='qualification-kind-lease')
        token = self.context.set((dict(job),fence,lease,failed))
        try:
            self._assert_work(self.binding['unitKind'],None,None)
            thread.start()
            yield {'jobId':job['jobId'],'leaseId':lease.lease_id,'kind':lease.capability_kind,'hostId':lease.host_id,'fence':fence}
        finally:
            self.context.reset(token)
            stop.set()
            if thread.ident is not None:thread.join(1)
            coordinator.release_lease(lease.lease_id)
            # Capacity remains retained until registered physical model/context
            # settlement, including cancellation or worker completion.
