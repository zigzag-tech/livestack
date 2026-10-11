"""Qualification-only local model guard backed by real retained claims and leases.

The app enters work() INSIDE the physically tracked worker, after computing the
exact request digest. Exiting this scope does not retire host/model capacity.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Thread
import os
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import stat
import subprocess

from .provider_installation import sha, private_config
from .provider_owner_probe import control, process_start_ticks

from .lease import CapabilityLeaseStore
from .provider_qualification import QualificationClient
from .qualification_reservations import QualificationReservationReader


class QualificationNodeGate:
    def __init__(self, manager, client, reader, binding, *, owner_socket, unit_sha256, capacity_url, nvml_sha256, activation_store):
        if not isinstance(client,QualificationClient) or not isinstance(reader,QualificationReservationReader) or not isinstance(manager.coordinator.broker,CapabilityLeaseStore):
            raise ValueError('actual_qualification_permission_claim_and_lease_components_required')
        if binding['identitySha256'] != client.source_digest or binding['configurationSha256'] != client.identity['configurationDigest'] or binding['unitKind'] not in manager.units or manager.coordinator.host_id != binding['hostId']:
            raise ValueError('qualification_node_source_kind_host_binding_required')
        self.manager,self.client,self.reader,self.binding = manager,client,reader,dict(binding)
        endpoint = urllib.parse.urlsplit(capacity_url)
        if endpoint.scheme!='http' or endpoint.hostname!='127.0.0.1' or endpoint.port is None or endpoint.path!='/qualification/admit' or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError('explicit_loopback_qualification_capacity_endpoint_required')
        self.nvml_sha256,self.activation_store = nvml_sha256,activation_store
        self.capacity_url = capacity_url
        self.owner_socket,self.unit_sha256 = owner_socket,unit_sha256
        self._verify_instance()
        self.uncertain = {}
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
        service_config,digest = private_config(self.client.settings['serviceConfigurationPath'])
        nodes = [entry.get('providerQualificationNode') for entry in service_config.get('apps',[]) if entry.get('descriptor',{}).get('app')==self.client.identity['app']]
        expected = {key:self.binding[key] for key in ('nodeId','authority','deviceId','unitKind','hostId')}
        expected.update(capacityUrl=self.capacity_url,reservationPath=str(self.reader.path),ownerSocket=self.owner_socket,unitSha256=self.unit_sha256,authorizationClient=self.client.settings,nvmlSha256=self.nvml_sha256,activationStore=self.activation_store)
        if nodes != [expected] or digest != self.client.identity['configurationDigest']:
            raise PermissionError('qualification_actual_private_node_constraints_changed')
        scope = configuration['intent']['scope']
        from .qualification_device import qualification_cuda_device
        actual=qualification_cuda_device(os.environ.get('CUDA_VISIBLE_DEVICES'),self.binding['deviceId'],self.nvml_sha256)
        if os.environ.get(self.client.identity['app'].upper()+'_QUALIFICATION_DEVICE_ID')!=actual or os.environ.get(self.client.identity['app'].upper()+'_QUALIFICATION_ACTIVATION_STORE')!=self.activation_store:
            raise PermissionError('qualification_actual_device_cache_environment_changed')
        service = scope['service']
        if service not in ('polytts-qualification.service','polyasr-qualification.service') or configuration['intent'].get('target') != self.binding['hostId'] or configuration['ssh']['expectedAuthority'] != self.binding['authority']:
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

    def admit(self, job, authorization):
        """Read-only control transport creates recognized retained custody only.
        A lost reply never starts a model; replay asks for the same exact claim."""
        if self.uncertain:raise PermissionError('qualification_physical_settlement_uncertain')
        self._verify_instance()
        self.client.authorize(effect='model-submit',job_id=job['jobId'],owner_id=job['ownerId'],input_digest=job['inputDigest'],model_kind=job['modelKind'],authorization=authorization)
        payload = {'nodeId':self.binding['nodeId'],**job}
        request = urllib.request.Request(self.capacity_url,data=json.dumps(payload,separators=(',',':')).encode(),headers={'Content-Type':'application/json','Authorization':authorization},method='POST')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(request,timeout=20) as response:
            content = response.read(16385)
            if len(content)>16384:raise PermissionError('qualification_capacity_receipt_bound')
        receipt = json.loads(content)
        if not isinstance(receipt,dict) or set(receipt)!={'nodeId','jobId','state','fence','replayed','binding','job','resourceLease','modelLoaded'} or receipt['nodeId']!=self.binding['nodeId'] or receipt['jobId']!=job['jobId'] or receipt['state']!='reserved' or type(receipt['replayed']) is not bool or receipt['binding']!=self.binding or receipt['job']!=job or receipt['resourceLease'] is not False or receipt['modelLoaded'] is not False:
            raise PermissionError('qualification_actual_capacity_receipt_changed')
        self.reader.assert_reserved(self.binding,job,receipt['fence'])
        return receipt

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
    def work(self, job, authorization, fence, *, synchronize):
        if not callable(synchronize):raise ValueError('qualification_real_physical_barrier_required')
        if self.uncertain:raise PermissionError('qualification_physical_settlement_uncertain')
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
        proof = {'jobId':job['jobId'],'leaseId':lease.lease_id,'kind':lease.capability_kind,
            'hostId':lease.host_id,'ownerId':lease.owner_id,'fence':fence,
            'leaseExpiresAt':lease.expires_at,'clockBasis':'coordinator-monotonic',
            'activeAtEntry':True,'physicalBarrier':'pending','leaseRelease':'pending'}
        try:
            self._assert_work(self.binding['unitKind'],None,None)
            thread.start()
            yield proof
        finally:
            try:
                # OwnedExecutorJobs has an outer barrier too; this one is
                # mandatory before releasing this actual kind lease.
                if synchronize() is not None:
                    raise PermissionError('qualification_physical_barrier_void_contract_required')
            except BaseException:
                proof['physicalBarrier'] = 'unknown'
                self.uncertain[job['jobId']] = (stop,thread,lease)
                raise
            else:
                proof['physicalBarrier'] = 'settled'
                heartbeat_failed = failed.is_set()
                try:
                    if coordinator.release_lease(lease.lease_id) is not True or any(value.lease_id == lease.lease_id for value in coordinator.broker.list_leases()):
                        raise PermissionError('qualification_kind_lease_release_unconfirmed')
                except BaseException:
                    proof['leaseRelease'] = 'unknown'
                    self.uncertain[job['jobId']] = (stop,thread,lease)
                    raise
                else:
                    proof['leaseRelease'] = 'released'
                    stop.set()
                    if thread.ident is not None:thread.join(1)
                    if heartbeat_failed:
                        raise PermissionError('qualification_lease_heartbeat_failed')
            finally:
                self.context.reset(token)
            # Model/host capacity remains retained even after job settlement.

    def settle_uncertain_owned(self, fence, synchronize):
        """Private owner lifecycle only; never an incoming model/control verdict."""
        from .provider_fence import ProviderFence
        if not isinstance(fence,ProviderFence):raise PermissionError('qualification_actual_owner_fence_required')
        fence.require_owned_shutdown()
        self._verify_instance()
        if synchronize() is not None:
            raise PermissionError('qualification_physical_barrier_void_contract_required')
        for job_id,(stop,thread,lease) in list(self.uncertain.items()):
            stop.set()
            if thread.ident is not None:thread.join(1)
            self.manager.coordinator.release_lease(lease.lease_id)
            del self.uncertain[job_id]
