"""CPU ownership controls only: no installed authority or remote GPU proof."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import unittest

from livestack_node.broker_owner import BrokerOwnerGate
from livestack_node.provider_fence import FenceRefused


def authorize(credential, action):
    if credential != 'fixture-owner':
        raise PermissionError('fixture authorization refused')


class BrokerOwnerTests(unittest.TestCase):
    def test_caller_cancellation_keeps_real_future_owned(self):
        gate = BrokerOwnerGate(SimpleNamespace(_in_flight={}), authorize)
        started, release = Event(), Event()
        def work():
            started.set()
            release.wait(2)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = gate.executor(executor).submit(work)
            self.assertTrue(started.wait(1))
            self.assertFalse(future.cancel())
            gate.fence.hold('fixture-owner', 'upgrade')
            self.assertFalse(gate.fence.status('fixture-owner')['drained'])
            with self.assertRaises(FenceRefused):
                gate.executor(executor).submit(lambda: None)
            release.set()
            future.result(timeout=1)
        self.assertTrue(gate.fence.status('fixture-owner')['drained'])

    def test_successful_remote_reply_is_not_physical_settlement(self):
        peer = SimpleNamespace(warm=lambda kind: {'resident': [kind]})
        broker = SimpleNamespace(_in_flight={}, peers=[peer],
            plan_and_apply=lambda: peer.warm('voxcpm'), sweep_leaks=lambda: [])
        gate = BrokerOwnerGate(broker, authorize)
        gate.install_dispatch()
        broker.plan_and_apply()
        gate.fence.hold('fixture-owner', 'upgrade')
        self.assertFalse(gate.fence.status('fixture-owner')['drained'])
        broker._in_flight.clear()  # Snapshot TTL is not a remote barrier.
        self.assertEqual(gate.fence.status('fixture-owner')['uncertain'], 1)

    def test_before_dispatch_absence_and_failed_authorization(self):
        error = OSError('fixture dial refused')
        error.dispatched = False
        def warm(kind):
            raise error
        peer = SimpleNamespace(warm=warm)
        def planning():
            try:
                peer.warm('voxcpm')
            except OSError:
                return []
        broker = SimpleNamespace(_in_flight={}, peers=[peer],
            plan_and_apply=planning, sweep_leaks=lambda: [])
        gate = BrokerOwnerGate(broker, authorize)
        gate.install_dispatch()
        broker.plan_and_apply()
        with self.assertRaises(PermissionError):
            gate.fence.hold('wrong-owner', 'upgrade')
        gate.fence.hold('fixture-owner', 'upgrade')
        self.assertTrue(gate.fence.status('fixture-owner')['drained'])
        with self.assertRaises(FenceRefused):
            broker.sweep_leaks()

    def test_changed_peer_inventory_cannot_bypass_remote_accounting(self):
        broker = SimpleNamespace(_in_flight={}, peers=[],
            plan_and_apply=lambda: self.fail('dispatch must not execute'), sweep_leaks=lambda: [])
        gate = BrokerOwnerGate(broker, authorize)
        gate.install_dispatch()
        broker.peers.append(SimpleNamespace(warm=lambda: None))
        with self.assertRaisesRegex(FenceRefused, 'peer_inventory_changed'):
            broker.plan_and_apply()


if __name__ == '__main__':
    unittest.main()
