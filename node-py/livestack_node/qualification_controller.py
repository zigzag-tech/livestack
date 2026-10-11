"""Configured canonical qualification permission plus recognized capacity custody.

Private records constrain registered owner identities; they grant no authority.
This controller never loads a model or calls the normal eviction dispatcher.
"""
import math
from pathlib import Path
import stat
import subprocess

from .planner import Unit
from .provider_installation import private_config, sha
from .provider_owner_probe import control, process_start_ticks
from .provider_qualification import QualificationClient


class QualificationController:
    def __init__(self, configuration_path, broker, *, reservation_path=None):
        from .qualification_reservations import QualificationReservationStore
        if reservation_path is None and broker.qualification_reservations is None:
            raise ValueError('durable_qualification_store_required')
        if reservation_path is not None and broker.qualification_reservations is not None:
            raise ValueError('qualification_store_already_configured')
        config, self.configuration_digest = private_config(configuration_path)
        if set(config) != {'version','nodes'} or type(config['version']) is not int or config['version'] != 1 or not isinstance(config['nodes'],list) or not 1 <= len(config['nodes']) <= 8:
            raise ValueError('closed_qualification_controller_configuration_required')
        self.configuration_path, self.broker = configuration_path, broker
        self.nodes = {}
        for entry in config['nodes']:
            if not isinstance(entry,dict) or set(entry) != {'binding','sourceIdentity','unit','ownerSocket','service','unitSha256','clientSettings'}:
                raise ValueError('closed_registered_qualification_node_required')
            node = entry['binding']
            QualificationReservationStore._validate(node,dict(jobId='00000000-0000-4000-8000-000000000000',ownerId='validation-only',inputDigest='0'*64,modelKind='voxcpm'))
            if entry['service'] not in ('polytts-qualification.service','polyasr-qualification.service') or set(entry['unit']) != {'kind','footprint'} or entry['unit']['kind'] != node['unitKind']:
                raise ValueError('qualification_fixed_unit_required')
            client = QualificationClient(entry['clientSettings'],entry['sourceIdentity'])
            client_config, client_config_digest = private_config(entry['clientSettings']['configurationPath'])
            if client_config_digest != entry['clientSettings']['configurationSha256']:
                raise ValueError('qualification_client_configuration_changed')
            if client_config['intent']['scope']['service'] != entry['service'] or client_config['intent']['targetMachine'] != node['hostId'] or client_config['ssh']['expectedAuthority'] != node['authority']:
                raise ValueError('qualification_registered_service_changed')
            if node['identitySha256'] != client.source_digest or node['configurationSha256'] != entry['sourceIdentity']['configurationDigest']:
                raise ValueError('qualification_registered_source_binding_changed')
            if node['nodeId'] in self.nodes:
                raise ValueError('qualification_duplicate_node')
            footprint = entry['unit']['footprint']
            if not isinstance(footprint,dict) or not footprint or any(not isinstance(key,str) or not key or isinstance(amount,bool) or not isinstance(amount,(int,float)) or not math.isfinite(amount) or amount <= 0 for key,amount in footprint.items()):
                raise ValueError('qualification_registered_positive_footprint_required')
            unit = Unit(node['unitKind'],footprint,servable_on=frozenset({node['deviceId']}))
            self.nodes[node['nodeId']] = (entry,client,unit)
        # Validate every configured source/client before adopting the durable
        # writer. An invalid startup cannot change retained claim ownership.
        if reservation_path is not None:
            self.store = QualificationReservationStore(reservation_path)
            broker.qualification_reservations = self.store
        else:
            self.store = broker.qualification_reservations

    def _verify_peer(self, entry):
        node = entry['binding']
        if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != node['bootId']:
            raise PermissionError('qualification_node_boot_changed')
        unit_path = Path('/etc/systemd/system')/entry['service']
        info = unit_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or sha(unit_path.read_bytes()) != entry['unitSha256']:
            raise PermissionError('qualification_registered_unit_bytes_changed')
        observed = subprocess.run(['/usr/bin/systemctl','show',entry['service'],'--property=MainPID,InvocationID,FragmentPath,ControlGroup,ActiveState,SubState'],capture_output=True,text=True,timeout=20,check=True)
        if len(observed.stdout)>8192: raise PermissionError('qualification_unit_observation_bound')
        fields = dict(line.split('=',1) for line in observed.stdout.splitlines() if '=' in line)
        pid = int(fields['MainPID'])
        if pid <= 0 or fields['InvocationID'] != node['unitInvocationId'] or fields['FragmentPath'] != str(unit_path) or fields['ControlGroup'] != '/system.slice/'+entry['service'] or fields['ActiveState'] != 'active' or fields['SubState'] != 'running' or str(process_start_ticks(pid)) != node['processStartTicks']:
            raise PermissionError('qualification_actual_unit_incarnation_changed')
        actual_cgroups = (Path('/proc')/str(pid)/'cgroup').read_text().splitlines()
        if '0::/system.slice/'+entry['service'] not in actual_cgroups:
            raise PermissionError('qualification_actual_process_cgroup_changed')
        result = control(entry['ownerSocket'],{'operation':'status'})
        peer,receipt = result['peer'],result['receipt']['result']
        if peer['pid'] != pid or str(peer['startTicks']) != node['processStartTicks'] or peer['presentAfterReply'] is not True or receipt['serverInstanceId'] != node['serverInstanceId'] or receipt['sourceIdentity'] != entry['sourceIdentity']:
            raise PermissionError('qualification_actual_owner_peer_changed')

    def admit(self, payload, authorization):
        if not isinstance(payload,dict) or set(payload) != {'nodeId','jobId','ownerId','inputDigest','modelKind'}:
            raise ValueError('closed_qualification_admission_request_required')
        if not isinstance(authorization,str) or not authorization.startswith('Bearer zzops_'):
            raise PermissionError('distinct_canonical_qualification_caller_required')
        _, digest = private_config(self.configuration_path)
        if digest != self.configuration_digest:
            raise PermissionError('qualification_controller_configuration_changed')
        if payload['nodeId'] not in self.nodes:
            raise PermissionError('qualification_registered_node_required')
        entry, client, unit = self.nodes[payload['nodeId']]
        node = entry['binding']
        job = {key:payload[key] for key in ('jobId','ownerId','inputDigest','modelKind')}
        expected_kind = 'voxcpm' if entry['service']=='polytts-qualification.service' else 'asr'
        if job['modelKind'] != expected_kind:
            raise PermissionError('qualification_model_service_mismatch')
        self.store._validate(node,job)
        # Verify even an exact repeated claim: a retained receipt cannot grant
        # access after the credential is revoked or the node is replaced.
        self._verify_peer(entry)
        client.authorize(effect='residency',job_id=job['jobId'],owner_id=job['ownerId'],input_digest=job['inputDigest'],model_kind=job['modelKind'],authorization=authorization)
        def snapshot():
            self._verify_peer(entry)
            world = self.broker.snapshot(_qualification_claims=False)
            client.authorize(effect='residency',job_id=job['jobId'],owner_id=job['ownerId'],input_digest=job['inputDigest'],model_kind=job['modelKind'],authorization=authorization)
            return world
        receipt = self.store.reserve(snapshot,unit,node,job)
        return {**receipt,'binding':node,'job':job,'resourceLease':False,'modelLoaded':False}
