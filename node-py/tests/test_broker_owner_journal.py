"""Real private SQLite fixtures; no authority, remote work or GPU effects."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import tempfile
import unittest
from uuid import uuid4
from types import SimpleNamespace

from livestack_node.broker_owner_journal import BrokerOwnerJournal
from livestack_node.broker_owner import BrokerOwnerGate
from livestack_node.provider_fence import FenceRefused


class BrokerJournalTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name) / 'effects.sqlite3')
        self.effect = {'kind': 'warm', 'peerId': 'fixture-node', 'requestSha256': 'a' * 64}

    def tearDown(self):
        self.folder.cleanup()

    def test_reopen_keeps_unknown_and_cannot_dispatch_or_settle_old_incarnation(self):
        journal = BrokerOwnerJournal(self.path, 'b' * 64, 'fixture-boot:1:10')
        nonce = uuid4().hex
        journal.intent(nonce, self.effect)
        reopened = BrokerOwnerJournal(self.path, 'b' * 64, 'fixture-boot:2:20')
        self.assertEqual(len(reopened.unsettled()), 1)
        with self.assertRaisesRegex(ValueError, 'existing_intent'):
            reopened.intent(nonce, self.effect)
        with self.assertRaisesRegex(ValueError, 'physical_settlement'):
            reopened.settle_local(nonce, self.effect)
        with self.assertRaisesRegex(ValueError, 'cas_refused'):
            reopened.known_not_dispatched(nonce, self.effect)
        with self.assertRaisesRegex(ValueError, 'source_changed'):
            BrokerOwnerJournal(self.path, 'c' * 64, 'fixture-boot:2:20')

    def test_actual_concurrent_nonce_cas_and_local_completion(self):
        journal = BrokerOwnerJournal(self.path, 'b' * 64, 'fixture-boot:1:10')
        nonce = uuid4().hex
        effect = {**self.effect, 'kind': 'local-work'}
        def claim():
            try:
                journal.intent(nonce, effect)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(lambda _: claim(), range(12)))
        self.assertEqual(sum(outcomes), 1)
        with self.assertRaisesRegex(ValueError, 'cas_refused'):
            journal.settle_local(nonce, {**effect, 'requestSha256': 'd' * 64})
        journal.settle_local(nonce, effect)
        self.assertEqual(journal.unsettled(), [])
        with self.assertRaisesRegex(ValueError, 'cas_refused'):
            journal.settle_local(nonce, effect)

    def test_bound_and_private_custody(self):
        journal = BrokerOwnerJournal(self.path, 'b' * 64, 'fixture-boot:1:10')
        for _ in range(64):
            journal.intent(uuid4().hex, self.effect)
        with self.assertRaisesRegex(ValueError, 'record_bound'):
            journal.intent(uuid4().hex, self.effect)
        self.assertEqual(len(journal.unsettled()), 64)
        os.chmod(self.path, 0o644)
        with self.assertRaisesRegex(PermissionError, 'private_file'):
            BrokerOwnerJournal(self.path, 'b' * 64, 'fixture-boot:1:10')
        os.chmod(self.path, 0o600)
        alias = Path(self.folder.name) / 'alias.sqlite3'
        alias.symlink_to(self.path)
        with self.assertRaisesRegex(PermissionError, 'absolute_private_path'):
            BrokerOwnerJournal(str(alias), 'b' * 64, 'fixture-boot:1:10')

    def test_connected_gate_commits_intent_before_remote_then_reopen_holds(self):
        journal = BrokerOwnerJournal(self.path, 'b' * 64, 'fixture-boot:1:10')
        def warm(kind):
            rows = journal.unsettled()
            self.assertEqual(len(rows), 2)  # Local planning plus remote effect.
            self.assertTrue(any('"kind":"warm"' in row['identity'] for row in rows))
            return {'resident': [kind]}
        peer = SimpleNamespace(base='http://fixture-owner.invalid/livestack', warm=warm)
        broker = SimpleNamespace(_in_flight={}, peers=[peer],
            plan_and_apply=lambda: peer.warm('voxcpm'), sweep_leaks=lambda: [])
        gate = BrokerOwnerGate(broker, lambda *_: None, journal=journal)
        gate.install_dispatch()
        broker.plan_and_apply()
        self.assertEqual(len(journal.unsettled()), 1)
        reopened = BrokerOwnerJournal(self.path, 'b' * 64, 'fixture-boot:2:20')
        other = BrokerOwnerGate(SimpleNamespace(_in_flight={}), lambda *_: None, journal=reopened)
        with self.assertRaisesRegex(FenceRefused, 'unsettled_reconciliation'):
            other.run(lambda: self.fail('unknown dispatch must not rerun'))


if __name__ == '__main__':
    unittest.main()
