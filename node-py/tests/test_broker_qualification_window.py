"""Actual ASGI/fence lifetime; synthetic permission/controller, no models/units."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from livestack_node.broker_owner import BrokerOwnerGate
from livestack_node.broker_owner_journal import BrokerOwnerJournal
from livestack_node.hostbroker import HostBroker
from livestack_node.hostd import build_app
from livestack_node.provider_fence import FenceRefused
from livestack_node.qualification_controller import QualificationController


class BrokerQualificationWindowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.broker = HostBroker([], [])
        self.revoked = False
        self.caller_revoked = False
        def authorize(credential, action):
            if credential != 'fixture-owner' or self.revoked:
                raise PermissionError('synthetic owner revoked')
        self.gate = BrokerOwnerGate(self.broker, authorize, journal=BrokerOwnerJournal(
            str(Path(self.folder.name) / 'effects.sqlite'), 'a' * 64, 'fixture'))
        # This fixture supplies no canonical, capacity or residency receipt.
        controller = QualificationController.__new__(QualificationController)
        controller.broker = self.broker
        def admit(payload, authorization):
            if authorization != 'Bearer fixture-caller' or self.caller_revoked:
                raise PermissionError('synthetic caller refused')
            return {'syntheticCpuAdmissionOnly': True}
        controller.admit = admit
        with patch.dict('os.environ', {'LIVESTACK_REPLAN_INTERVAL': '0'}):
            app = build_app(self.broker, qualification=controller, owner_gate=self.gate)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture.invalid')

    async def asyncTearDown(self):
        self.gate.fence._cancel_qualification_locked()
        if self.gate._async_pool:
            self.gate._async_pool.shutdown(wait=True)
        await self.client.aclose()
        self.folder.cleanup()

    async def test_window_allows_only_authenticated_qualification_and_settlement_reads(self):
        held = self.gate.fence.hold('fixture-owner', 'fixture')
        self.gate.fence.release_for_qualification('fixture-owner', 'fixture', held['epoch'], 2)
        self.assertEqual((await self.client.post('/admit', json={'kind': 'voxcpm'})).status_code, 503)
        self.assertEqual((await self.client.post('/peers', json={})).status_code, 503)
        self.assertEqual((await self.client.post('/qualification/admit', json={})).status_code, 403)
        response = await self.client.post('/qualification/admit', json={}, headers={'authorization': 'Bearer fixture-caller'})
        self.assertEqual(response.json(), {'syntheticCpuAdmissionOnly': True})
        self.caller_revoked = True
        self.assertEqual((await self.client.post('/qualification/admit', json={}, headers={
            'authorization': 'Bearer fixture-caller'})).status_code, 403)
        self.assertEqual((await self.client.get('/status')).status_code, 200)
        self.assertEqual((await self.client.post('/lease/missing/release', json={})).status_code, 200)
        with self.assertRaisesRegex(FenceRefused, 'normal_dispatch_refused'):
            self.broker.plan_and_apply(None)
        self.assertEqual(self.gate.journal.unsettled(), [])

    async def test_stale_revoked_expired_and_ordinary_release_paths(self):
        held = self.gate.fence.hold('fixture-owner', 'fixture')
        with self.assertRaises(FenceRefused):
            self.gate.fence.release_for_qualification('fixture-owner', 'fixture', held['epoch'] + 1, 1)
        self.revoked = True
        with self.assertRaises(PermissionError):
            self.gate.fence.release_for_qualification('fixture-owner', 'fixture', held['epoch'], 1)
        self.revoked = False
        self.gate.fence.release_for_qualification('fixture-owner', 'fixture', held['epoch'], 1)
        await asyncio.sleep(1.05)
        self.assertEqual((await self.client.post('/qualification/admit', json={}, headers={'authorization': 'Bearer fixture-caller'})).status_code, 503)
        held = self.gate.fence.status('fixture-owner')
        self.assertEqual(held['holder'], 'fixture')
        self.assertTrue(held['drained'])
        self.gate.fence.release('fixture-owner', 'fixture', held['epoch'])
        self.assertNotEqual((await self.client.post('/admit', json={'kind': 'voxcpm'})).status_code, 503)


if __name__ == '__main__':
    unittest.main()
