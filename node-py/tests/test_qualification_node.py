"""Real compiled ModelManager/SQLite/capability leases, CPU loader + mock canonical permission/kernel peer.

No service registration, physical GPU admission or inference qualification.
"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4
from livestack_node.manager import ManagedUnit,ModelManager
from livestack_node.freeing import noop_free
from livestack_node.coordinator import LivestackCoordinator
from livestack_node.planner import Device,Unit,WorldState
from livestack_node.provider_qualification import QualificationClient
from livestack_node.qualification_reservations import QualificationReservationStore,QualificationReservationReader
from livestack_node.qualification_node import QualificationNodeGate

class QualificationNodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=QualificationReservationStore(str(self.root/'claims.sqlite'))
        self.node=dict(nodeId='node',authority='auth',identitySha256='a'*64,configurationSha256='b'*64,serverInstanceId='server',bootId='boot',unitInvocationId='unit',processStartTicks='1',deviceId='gpu',unitKind='voxcpm',hostId='host')
        self.job=dict(jobId=str(uuid4()),ownerId='owner',inputDigest='c'*64,modelKind='voxcpm')
        world=WorldState(devices=(Device('gpu','host',{'vram_bytes':6}),),units={},placements=(),measured_free={'gpu':{'vram_bytes':6}},now=100)
        self.store.reserve(world,Unit('voxcpm',{'vram_bytes':4},servable_on=frozenset({'gpu'})),self.node,self.job)
        self.loads=[];self.coordinator=LivestackCoordinator('host',coload=False)
        self.manager=ModelManager({'voxcpm':ManagedUnit('voxcpm',loader=lambda:self.loads.append('voxcpm') or object(),freer=noop_free)},0,coordinator=self.coordinator)
        self.client=object.__new__(QualificationClient);self.client.source_digest='a'*64;self.client.identity={'configurationDigest':'b'*64};self.client.authorize=Mock(return_value={'fixture':'mock permission'})
        self.peer_patch=patch.object(QualificationNodeGate,'_verify_instance');self.peer_patch.start();self.addCleanup(self.peer_patch.stop)
        self.gate=QualificationNodeGate(self.manager,self.client,QualificationReservationReader(self.store.path),self.node,owner_socket='mock-peer',unit_sha256='e'*64)
    def tearDown(self):self.tmp.cleanup()
    def test_direct_ensure_and_load_cannot_bypass_admitted_work(self):
        for call in (self.manager.ensure,self.manager._load):
            with self.assertRaises(PermissionError):call('voxcpm')
        self.assertEqual(self.loads,[])
    def test_actual_kind_lease_precedes_cpu_load_and_capacity_is_retained(self):
        with self.gate.work(self.job,'Bearer fixture',1) as receipt:
            self.assertEqual(self.loads,[])
            leases=self.coordinator.broker.list_leases()
            self.assertEqual(len(leases),1);self.assertEqual(leases[0].lease_id,receipt['leaseId'])
            self.manager.ensure('voxcpm');self.assertEqual(self.loads,['voxcpm'])
        self.assertNotIn(receipt['leaseId'],[lease.lease_id for lease in self.coordinator.broker.list_leases()])
        self.gate.reader.assert_reserved(self.node,self.job,1)
        with self.assertRaises(PermissionError):self.manager.ensure('voxcpm')
    def test_forged_job_stale_fence_revoked_permission_and_lost_lease_refuse(self):
        for job,fence in ((dict(self.job,jobId=str(uuid4())),1),(dict(self.job,inputDigest='d'*64),1),(self.job,2)):
            with self.assertRaises(PermissionError):
                with self.gate.work(job,'Bearer fixture',fence):pass
        self.client.authorize.side_effect=PermissionError('mock revoked')
        with self.assertRaises(PermissionError):
            with self.gate.work(self.job,'Bearer fixture',1):pass
        self.client.authorize.side_effect=None
        with self.gate.work(self.job,'Bearer fixture',1) as receipt:
            self.coordinator.release_lease(receipt['leaseId'])
            with self.assertRaises(PermissionError):self.manager.ensure('voxcpm')
        self.assertEqual(self.loads,[])
    def test_context_does_not_leak_to_other_thread_and_accepted_worker_retains_lease(self):
        entered,finish=threading.Event(),threading.Event();observed=[]
        def worker():
            with self.gate.work(self.job,'Bearer fixture',1) as receipt:
                self.manager.ensure('voxcpm');observed.append(receipt);entered.set();finish.wait(1)
        thread=threading.Thread(target=worker);thread.start();self.assertTrue(entered.wait(1))
        try:
            self.assertIn(observed[0]['leaseId'],[lease.lease_id for lease in self.coordinator.broker.list_leases()])
            with self.assertRaises(PermissionError):self.manager.ensure('voxcpm')
        finally:finish.set();thread.join(2)
        self.gate.reader.assert_reserved(self.node,self.job,1)
    def test_wrong_device_budget_and_held_claim_refuse_before_loader(self):
        with self.gate.work(self.job,'Bearer fixture',1):
            for args in ({'device':'other'},{'budget':{'vram_bytes':100}}):
                with self.assertRaises(PermissionError):self.manager.ensure('voxcpm',**args)
            self.store.hold(self.node,1)
            with self.assertRaises(PermissionError):self.manager.ensure('voxcpm')
        self.assertEqual(self.loads,[])

if __name__=='__main__':unittest.main()
