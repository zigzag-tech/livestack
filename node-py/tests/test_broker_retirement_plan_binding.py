"""Actual disk journal guards; manager calls are mocked, never service effects."""
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from livestack_node.broker_retirement import BrokerRetirementJournal


class BrokerRetirementPlanBindingTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        files = []
        for role in ('source', 'unit', 'configuration', 'operations', 'ledger', 'lease-snapshot'):
            path = root / role
            path.write_bytes(b'fixture')
            files.append({'path': str(path), 'role': role, 'unit': 'livestack-hostd.service',
                          'size': 7, 'sha256': sha256(b'fixture').hexdigest()})
        unit = {'unit': 'livestack-hostd.service', 'role': 'broker', 'unitSha256': 'a' * 64,
                'bootId': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'invocationId': 'b' * 32,
                'mainPid': 42, 'startTicks': 123, 'controlGroup': '/system.slice/livestack-hostd.service',
                'acceptedWork': 'unknown', 'impact': 'operator-aborted-unknown-result'}
        self.journal = BrokerRetirementJournal(root / 'journal.sqlite',
            {'version': 1, 'mode': 'abort', 'units': [unit], 'retainedFiles': files}, files)

    def test_changed_loaded_plan_refuses_before_manager_or_journal_write(self):
        self.journal.plan['units'][0]['mainPid'] = 777
        with patch('livestack_node.broker_retirement.inspect_predecessor') as inspect:
            with self.assertRaisesRegex(ValueError, 'loaded_plan_changed'):
                self.journal.submit('livestack-hostd.service', {})
            inspect.assert_not_called()
        with sqlite3.connect(self.journal.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM units').fetchone(), (0,))

    def test_changed_durable_binding_refuses_submit_reconcile_and_status(self):
        with sqlite3.connect(self.journal.path) as db:
            db.execute("UPDATE binding SET plan=? WHERE id=1", ('f' * 64,))
        with patch('livestack_node.broker_retirement.inspect_predecessor') as inspect, \
             patch('livestack_node.broker_retirement.verify_terminated') as terminal:
            for operation in (lambda: self.journal.submit('livestack-hostd.service', {}),
                              lambda: self.journal.reconcile('livestack-hostd.service'),
                              self.journal.status):
                with self.assertRaisesRegex(ValueError, 'durable_binding_changed'):
                    operation()
            inspect.assert_not_called()
            terminal.assert_not_called()
        with sqlite3.connect(self.journal.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM units').fetchone(), (0,))


if __name__ == '__main__':
    unittest.main()
