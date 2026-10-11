"""Configured real broker/SQLite planning; effects use explicit CPU fake peers."""
import tempfile
import threading
import unittest
from pathlib import Path
from uuid import uuid4
from livestack_node.hostbroker import HostBroker
from livestack_node.planner import Device, Unit, Request, Placement
from livestack_node.qualification_reservations import QualificationReservationStore

class Peer:
    host_id='host'; device_id='gpu'
    def __init__(self): self.calls=[]; self.loaded=False
    def units(self): return {'normal':Unit('normal',{'vram_bytes':4})}
    def placements(self): return [Placement('normal','gpu',loaded_at=1)] if self.loaded else []
    def device_memory(self): return {'vram_bytes':6}
    def device_capacity(self): return {'vram_bytes':6}
    def warm(self,*args,**kwargs): self.calls.append('warm'); self.loaded=True
    def evict(self,*args): self.calls.append('evict'); self.loaded=False

class BrokerClaimsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=str(Path(self.tmp.name)/'custody.sqlite')
        self.store=QualificationReservationStore(self.path); self.peer=Peer()
        self.broker=HostBroker([Device('gpu','host',{'vram_bytes':6})],[self.peer],clock=lambda:100,qualification_reservations=self.store)
        self.unit=Unit('qualified',{'vram_bytes':4},servable_on=frozenset({'gpu'}))
        self.node=dict(nodeId='node',authority='auth',identitySha256='a'*64,configurationSha256='b'*64,serverInstanceId='server',bootId='boot',unitInvocationId='unit',processStartTicks='1',deviceId='gpu',unitKind='qualified',hostId='host')
        self.job=dict(jobId=str(uuid4()),ownerId='owner',inputDigest='c'*64,modelKind='voxcpm')
    def tearDown(self): self.tmp.cleanup()
    def claim(self): return self.store.reserve(lambda:self.broker.snapshot(_qualification_claims=False),self.unit,self.node,self.job)
    def test_normal_admission_counts_retained_claim(self):
        self.claim()
        self.assertEqual(self.broker.snapshot().devices[0].reserved['vram_bytes'],4)
        self.assertIsNone(self.broker.admit(Request('normal-job','normal',created_at=100)))
        self.assertEqual(self.peer.calls,[])
        self.store.hold(self.node,1)
        self.assertIsNone(self.broker.admit(Request('normal-job2','normal',created_at=100)))
    def test_unconfigured_normal_path_is_preserved(self):
        normal=HostBroker([Device('gpu','host',{'vram_bytes':6})],[self.peer],clock=lambda:100)
        self.assertEqual(normal.admit(Request('normal-job','normal',created_at=100)),'gpu')
        self.assertEqual(self.peer.calls,['warm'])
    def test_snapshot_claim_and_normal_dispatch_are_serialized(self):
        entered=threading.Event(); release=threading.Event(); completed=threading.Event(); errors=[]
        def supplier():
            entered.set(); release.wait(1)
            return self.broker.snapshot(_qualification_claims=False)
        def claim():
            try:self.store.reserve(supplier,self.unit,self.node,self.job)
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=claim);thread.start();self.assertTrue(entered.wait(1))
        result=[]
        normal=threading.Thread(target=lambda:(result.append(self.broker.admit(Request('normal','normal',created_at=100))),completed.set()))
        normal.start();self.assertFalse(completed.wait(.02));release.set();thread.join(2);normal.join(2)
        self.assertEqual(errors,[]);self.assertEqual(result,[None]);self.assertEqual(self.peer.calls,[])

if __name__=='__main__':unittest.main()
