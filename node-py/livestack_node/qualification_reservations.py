"""Transactional capacity custody, not caller authorization or a physical lease.

The configured host authority must verify canonical permission and the exact
node/kernel/source tuple before calling these domain methods. Retained claims
survive control-process restart as held, never automatically expire or rerun.
"""
from contextlib import closing
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import re
from uuid import UUID

from .fleet_operations import OperationStore
from .provider_owner_probe import process_start_ticks
from .qualification_capacity import plan_qualification_capacity


class QualificationReservationStore(OperationStore):
    def __init__(self, path):
        super().__init__(path, max_records=64, max_age_s=None)
        self.controller = ':'.join((Path('/proc/sys/kernel/random/boot_id').read_text().strip(), str(os.getpid()), str(process_start_ticks(os.getpid()))))
        with self._writer() as db:
            schema = '''CREATE TABLE IF NOT EXISTS qualification_controller (id INTEGER PRIMARY KEY CHECK(id=1), incarnation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qualification_nodes (node_id TEXT PRIMARY KEY, binding TEXT NOT NULL, footprint TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('reserved','held','retired')), fence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS qualification_jobs (node_id TEXT NOT NULL, job_id TEXT NOT NULL, binding TEXT NOT NULL, PRIMARY KEY(node_id,job_id));'''
            for statement in schema.split(';'):
                if statement.strip(): db.execute(statement)
            previous = db.execute('SELECT incarnation FROM qualification_controller WHERE id=1').fetchone()
            if previous is not None and previous[0] != self.controller:
                db.execute("UPDATE qualification_nodes SET state='held',fence=fence+1 WHERE state='reserved'")
            db.execute('INSERT INTO qualification_controller VALUES(1,?) ON CONFLICT(id) DO UPDATE SET incarnation=excluded.incarnation', (self.controller,))

    def _connect(self):
        db = super()._connect()
        db.execute('PRAGMA busy_timeout=1000')
        db.execute('PRAGMA synchronous=FULL')
        return db

    @staticmethod
    def _encode(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'))

    def _validate(self, node, job):
        fields = {'nodeId','authority','identitySha256','configurationSha256','serverInstanceId','bootId','unitInvocationId','processStartTicks','deviceId','unitKind','hostId'}
        if not isinstance(node, dict) or set(node) != fields or any(not isinstance(value, str) or not value or len(value)>192 for value in node.values()) or len(self._encode(node))>4096:
            raise ValueError('qualification_exact_node_binding_required')
        if any(not re.fullmatch('[a-f0-9]{64}', node[key]) for key in ('identitySha256','configurationSha256')):
            raise ValueError('qualification_source_configuration_digest_required')
        if not isinstance(job, dict) or set(job) != {'jobId','ownerId','inputDigest','modelKind'} or not isinstance(job['ownerId'], str) or not job['ownerId'].strip() or len(job['ownerId'])>192 or job['modelKind'] not in ('voxcpm','asr') or not isinstance(job['inputDigest'], str) or not re.fullmatch('[a-f0-9]{64}', job['inputDigest']):
            raise ValueError('qualification_exact_job_binding_required')
        if not isinstance(job['jobId'], str): raise ValueError('qualification_job_uuid_required')
        UUID(job['jobId'])

    def _overlay(self, world, rows):
        reserved = {device.id: dict(device.reserved) for device in world.devices}
        hosts = {host: dict(resources) for host,resources in world.hosts.items()}
        for row in rows:
            if row['state']=='retired': continue
            node, footprint = json.loads(row['binding']), json.loads(row['footprint'])
            device = next((device for device in world.devices if device.id==node['deviceId'] and device.host_id==node['hostId']), None)
            if device is None: raise ValueError('qualification_retained_device_unobserved')
            for key,amount in footprint.items():
                if key in device.capacity: reserved[device.id][key]=reserved[device.id].get(key,0)+amount
                elif node['hostId'] in hosts and key in hosts[node['hostId']]: hosts[node['hostId']][key]=max(0,hosts[node['hostId']][key]-amount)
                else: raise ValueError('qualification_retained_resource_unobserved')
        return replace(world, devices=tuple(replace(device,reserved=reserved[device.id]) for device in world.devices), hosts=hosts)

    def apply_to_world(self, world):
        with closing(self._connect()) as db:
            return self._overlay(world, db.execute('SELECT * FROM qualification_nodes').fetchall())

    def _current_controller(self, db):
        row = db.execute('SELECT incarnation FROM qualification_controller WHERE id=1').fetchone()
        if row is None or row[0] != self.controller:
            raise ValueError('qualification_controller_incarnation_changed')

    def reserve(self, world, unit, node, job):
        self._validate(node,job)
        node_text, job_text = self._encode(node), self._encode(job)
        if unit.kind != node['unitKind']: raise ValueError('qualification_kind_binding_changed')
        footprint = dict(unit.admission_footprint or unit.footprint)
        if not footprint or any(isinstance(amount,bool) or not isinstance(amount,(int,float)) or not math.isfinite(amount) or amount<=0 for amount in footprint.values()):
            raise ValueError('qualification_positive_resource_claim_required')
        with self._writer() as db:
            self._current_controller(db)
            existing = db.execute('SELECT * FROM qualification_nodes WHERE node_id=?',(node['nodeId'],)).fetchone()
            previous = db.execute('SELECT binding FROM qualification_jobs WHERE node_id=? AND job_id=?',(node['nodeId'],job['jobId'])).fetchone()
            if previous is not None and previous[0] != job_text: raise ValueError('qualification_job_identity_changed')
            if existing is not None:
                if existing['binding']!=node_text or existing['footprint']!=self._encode(footprint): raise ValueError('qualification_node_identity_changed')
                if existing['state']!='reserved': raise ValueError('qualification_retained_claim_held_or_retired')
                fence=existing['fence']
            else:
                if db.execute('SELECT count(*) FROM qualification_nodes').fetchone()[0]>=16: raise ValueError('qualification_node_record_bound')
                observed = self._overlay(world,db.execute('SELECT * FROM qualification_nodes').fetchall())
                plan_qualification_capacity(observed,unit,node['deviceId'],job['jobId'],job['ownerId'])
                fence=1
                db.execute('INSERT INTO qualification_nodes VALUES(?,?,?,?,?)',(node['nodeId'],node_text,self._encode(footprint),'reserved',fence))
            if previous is None:
                if db.execute('SELECT count(*) FROM qualification_jobs').fetchone()[0]>=64: raise ValueError('qualification_job_record_bound')
                db.execute('INSERT INTO qualification_jobs VALUES(?,?,?)',(node['nodeId'],job['jobId'],job_text))
            return {'nodeId':node['nodeId'],'jobId':job['jobId'],'state':'reserved','fence':fence,'replayed':previous is not None}

    def hold(self, node, fence):
        with self._writer() as db:
            self._current_controller(db)
            count=db.execute("UPDATE qualification_nodes SET state='held',fence=fence+1 WHERE node_id=? AND binding=? AND fence=? AND state='reserved'",(node['nodeId'],self._encode(node),fence)).rowcount
            if count!=1: raise ValueError('qualification_hold_cas_refused')
            return fence+1

    def retire_verified(self, node, fence):
        """Storage CAS only; the registered controller must first verify actual
        candidate-only model/cgroup/GPU settlement. No public JSON verdict is
        accepted by this method, and no endpoint should expose it directly."""
        with self._writer() as db:
            self._current_controller(db)
            count=db.execute("UPDATE qualification_nodes SET state='retired',fence=fence+1 WHERE node_id=? AND binding=? AND fence=? AND state='held'",(node['nodeId'],self._encode(node),fence)).rowcount
            if count!=1: raise ValueError('qualification_retirement_cas_refused')
