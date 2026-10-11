"""Real SQLite custody controls; no authority registration or physical effects."""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from livestack_node.planner import Device, Unit, WorldState
from livestack_node.qualification_reservations import QualificationReservationStore, QualificationReservationReader

class ReservationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'custody.sqlite')
        self.store = QualificationReservationStore(self.path)
        self.unit = Unit('qualified', {'vram_bytes': 4}, servable_on=frozenset({'gpu'}))
        self.world = WorldState(devices=(Device('gpu','host',{'vram_bytes': 6}),), units={}, placements=(), measured_free={'gpu':{'vram_bytes':6}}, now=100)
        self.node = dict(nodeId='node',authority='auth',identitySha256='a'*64,configurationSha256='b'*64,serverInstanceId='server',bootId='boot',unitInvocationId='unit',processStartTicks='1',deviceId='gpu',unitKind='qualified',hostId='host')
        self.job = dict(jobId=str(uuid4()),ownerId='owner',inputDigest='c'*64,modelKind='voxcpm')
    def tearDown(self): self.tmp.cleanup()
    def test_exact_replay_reopen_and_changed_identity(self):
        self.assertFalse(self.store.reserve(self.world,self.unit,self.node,self.job)['replayed'])
        reopened = QualificationReservationStore(self.path)
        self.assertTrue(reopened.reserve(self.world,self.unit,self.node,self.job)['replayed'])
        for node,job in ((dict(self.node,serverInstanceId='other'),self.job),(self.node,dict(self.job,inputDigest='d'*64))):
            with self.assertRaises(ValueError): reopened.reserve(self.world,self.unit,node,job)
    def test_concurrent_claims_cannot_overbook(self):
        def claim(i):
            try:
                self.store.reserve(self.world,self.unit,dict(self.node,nodeId=str(i)),dict(self.job,jobId=str(uuid4())))
                return True
            except ValueError: return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(claim,range(8))),1)
    def test_restart_holds_and_old_controller_cannot_mutate(self):
        self.store.reserve(self.world,self.unit,self.node,self.job)
        with patch('livestack_node.qualification_reservations.process_start_ticks',return_value=999999):
            restarted=QualificationReservationStore(self.path)
        with self.assertRaisesRegex(ValueError,'held_or_retired'):
            restarted.reserve(self.world,self.unit,self.node,self.job)
        with self.assertRaisesRegex(ValueError,'incarnation_changed'): self.store.hold(self.node,1)
        with self.assertRaises(ValueError): restarted.retire_verified(self.node,1)
        # Restart advanced fence2; retirement is mechanical CAS, no physical proof here.
        restarted.retire_verified(self.node,2)
    def test_hold_cas_and_unknown_retained_device(self):
        self.store.reserve(self.world,self.unit,self.node,self.job)
        self.assertEqual(self.store.hold(self.node,1),2)
        with self.assertRaises(ValueError): self.store.hold(self.node,1)
        with self.assertRaisesRegex(ValueError,'unobserved'):
            self.store.apply_to_world(replace(self.world,devices=()))
        self.assertEqual(self.store.apply_to_world(self.world).devices[0].reserved['vram_bytes'],4)
    def test_readonly_claim_view_cannot_mint_or_adopt_claims(self):
        reader=QualificationReservationReader(self.path)
        with self.assertRaises(PermissionError):reader.assert_reserved(self.node,self.job,1)
        self.store.reserve(self.world,self.unit,self.node,self.job)
        self.assertEqual(reader.assert_reserved(self.node,self.job,1)['fence'],1)
        for job,fence in ((dict(self.job,inputDigest='d'*64),1),(self.job,True),(self.job,2)):
            with self.assertRaises(PermissionError):reader.assert_reserved(self.node,job,fence)
        self.assertTrue(self.store.reserve(self.world,self.unit,self.node,self.job)['replayed'])
        self.store.hold(self.node,1)
        with self.assertRaises(PermissionError):reader.assert_reserved(self.node,self.job,1)
    def test_changed_store_permissions_and_aliases_refuse(self):
        os.chmod(self.path,0o644)
        with self.assertRaises(PermissionError):QualificationReservationStore(self.path)
        with self.assertRaises(PermissionError):QualificationReservationReader(self.path).assert_reserved(self.node,self.job,1)
        os.chmod(self.path,0o600)
        os.link(self.path,self.path+'.alias')
        with self.assertRaises(PermissionError):QualificationReservationStore(self.path)
    def test_malformed_job_refuses_before_records(self):
        for job in (dict(self.job,inputDigest=True),dict(self.job,jobId=None),dict(self.job,extra=True)):
            with self.assertRaises(ValueError): self.store.reserve(self.world,self.unit,self.node,job)
        self.assertEqual(self.store.apply_to_world(self.world).devices[0].reserved,{})

if __name__=='__main__': unittest.main()
