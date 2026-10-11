"""Actual ASGI/SQLite paths; canonical grants and systemd peers are explicit CPU mocks.

Real canonical PG/bundle controls are separate. These do not qualify registration,
physical node identity, inference, or a live resource lease.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4
import httpx
from livestack_node.hostbroker import HostBroker
from livestack_node.hostd import build_app
from livestack_node.planner import Device,Unit
from livestack_node.provider_installation import sha
from livestack_node.qualification_controller import QualificationController
from livestack_node.qualification_reservations import QualificationReservationStore
from test_qualification_broker import Peer

class QualificationControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=QualificationReservationStore(str(self.root/'claims.sqlite'));self.peer=Peer()
        self.broker=HostBroker([Device('gpu','host',{'vram_bytes':6})],[self.peer],clock=lambda:100,qualification_reservations=self.store)
        self.node=dict(nodeId='node',authority='auth',identitySha256='a'*64,configurationSha256='b'*64,serverInstanceId='server',bootId='boot',unitInvocationId='unit',processStartTicks='1',deviceId='gpu',unitKind='qualified',hostId='host')
        self.payload=dict(nodeId='node',jobId=str(uuid4()),ownerId='owner',inputDigest='c'*64,modelKind='voxcpm')
        self.config=self.root/'config.json';self.config.write_text('{}');self.config.chmod(0o600)
        self.client=Mock();self.client.authorize.return_value={'fixture':'mock canonical receipt; not a real grant'}
        self.controller=object.__new__(QualificationController)
        self.controller.configuration_path=str(self.config);self.controller.configuration_digest=sha(self.config.read_bytes())
        self.controller.broker=self.broker;self.controller.store=self.store
        self.controller.nodes={'node':({'binding':self.node,'service':'polytts-qualification.service'},self.client,Unit('qualified',{'vram_bytes':4},servable_on=frozenset({'gpu'})))}
        self.controller._verify_peer=Mock()
        self.app=build_app(self.broker,qualification=self.controller)
        self.token='Bearer zzops_'+'d'*64
    def tearDown(self): self.tmp.cleanup()
    def request(self,payload=None,token=None,raw=None,app=None):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app or self.app),base_url='http://fixture') as client:
                return await client.post('/qualification/admit',headers={'authorization':token or ''},json=payload) if raw is None else await client.post('/qualification/admit',content=raw,headers={'authorization':token or ''})
        return asyncio.run(run())
    def test_closed_route_is_absent_without_configuration(self):
        self.assertEqual(self.request(self.payload,self.token,app=build_app(self.broker)).status_code,404)
    def test_anonymous_and_normal_lease_tokens_do_not_reach_permission(self):
        for token in ('','Bearer fleet-control','Bearer job-control'):
            self.assertEqual(self.request(self.payload,token).status_code,403)
        self.client.authorize.assert_not_called();self.controller._verify_peer.assert_not_called()
    def test_exact_reservation_replay_and_revocation(self):
        first=self.request(self.payload,self.token);self.assertEqual(first.status_code,200,first.text)
        self.assertIs(first.json()['resourceLease'],False);self.assertIs(first.json()['modelLoaded'],False)
        self.assertEqual(self.peer.calls,[])
        self.assertIs(self.request(self.payload,self.token).json()['replayed'],True)
        self.client.authorize.side_effect=PermissionError('revoked fixture')
        self.assertEqual(self.request(self.payload,self.token).status_code,403)
    def test_wrong_node_configuration_peer_and_closed_fields_refuse(self):
        for payload in (dict(self.payload,nodeId='wrong'),dict(self.payload,extra=True),dict(self.payload,modelKind='asr')):
            self.assertIn(self.request(payload,self.token).status_code,(400,403))
        self.controller._verify_peer.side_effect=PermissionError('wrong physical peer fixture')
        self.assertEqual(self.request(self.payload,self.token).status_code,403)
        self.controller._verify_peer.side_effect=None
        self.config.write_text('{"changed":true}')
        self.assertEqual(self.request(self.payload,self.token).status_code,403)
        self.assertEqual(self.peer.calls,[])
    def test_raw_body_bound_and_malformed_json(self):
        self.assertEqual(self.request(token=self.token,raw=b' '*8193).status_code,413)
        self.assertEqual(self.request(token=self.token,raw=b'{').status_code,400)
        self.client.authorize.assert_not_called()
    def test_held_claim_does_not_reopen_after_successful_mock_permission(self):
        self.assertEqual(self.request(self.payload,self.token).status_code,200)
        self.store.hold(self.node,1)
        self.assertEqual(self.request(self.payload,self.token).status_code,400)
        self.assertEqual(self.peer.calls,[])

if __name__=='__main__':unittest.main()
