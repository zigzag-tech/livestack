"""Actual ASGI and thread cancellation; CPU-only synthetic broker fixture."""
import asyncio
from pathlib import Path
from threading import Event
import tempfile
import unittest
from unittest.mock import patch

import httpx

from livestack_node.broker_owner import BrokerOwnerGate
from livestack_node.broker_owner_journal import BrokerOwnerJournal
from livestack_node.hostbroker import HostBroker
from livestack_node.hostd import build_app
from livestack_node.provider_fence import FenceRefused


class BrokerOwnerHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.journal = BrokerOwnerJournal(str(Path(self.folder.name) / 'effects.sqlite'), 'b' * 64, 'fixture-boot:1:10')
        self.broker = HostBroker([], [])
        self.gate = BrokerOwnerGate(self.broker, lambda *_: None, journal=self.journal)
        with patch.dict('os.environ', {'LIVESTACK_REPLAN_INTERVAL': '0'}):
            self.app = build_app(self.broker, owner_gate=self.gate)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://fixture.invalid')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.folder.cleanup()

    async def test_held_ingress_keeps_existing_lease_settlement_and_reads(self):
        self.gate.fence.hold('fixture-owner', 'source-upgrade')
        self.assertEqual((await self.client.post('/admit', json={'kind': 'voxcpm'})).status_code, 503)
        self.assertEqual((await self.client.get('/status')).status_code, 200)
        reply = await self.client.post('/lease/missing/release', json={})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json(), {'ok': False})
        self.assertEqual(self.journal.unsettled(), [])

    async def test_cancelled_http_does_not_orphan_actual_sync_handler(self):
        entered, release = Event(), Event()
        def slow_snapshot(*args, **kwargs):
            entered.set()
            release.wait(2)
            raise ValueError('synthetic completed CPU handler')
        self.broker.plan_and_apply = slow_snapshot
        request = asyncio.create_task(self.client.post('/admit', json={'kind': 'voxcpm'}))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            request.cancel()
            try:
                await request
            except asyncio.CancelledError:
                pass
            self.gate.fence.hold('fixture-owner', 'source-upgrade')
            self.assertFalse(self.gate.fence.status('fixture-owner')['drained'])
            self.assertEqual(len(self.journal.unsettled()), 1)
        finally:
            release.set()
        # Wait on the fixture's actual handler completion, not a socket close.
        for _ in range(100):
            if self.gate.fence.status('fixture-owner')['drained']:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.gate.fence.status('fixture-owner')['drained'])
        self.assertEqual(self.journal.unsettled(), [])
        with self.assertRaises(FenceRefused):
            self.gate.shutdown_owned()


if __name__ == '__main__':
    unittest.main()
