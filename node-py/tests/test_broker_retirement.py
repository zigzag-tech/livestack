from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from livestack_node.broker_retirement import BrokerRetirementJournal


class BrokerRetirementTest(unittest.TestCase):
    def fixture(self, root):
        files = []
        for role in ('source', 'unit', 'configuration', 'operations', 'ledger', 'lease-snapshot'):
            path = root / role; path.write_bytes(b'CPU fixture')
            files.append({'path': str(path), 'role': role, 'unit': 'livestack-hostd.service',
                          'size': 11, 'sha256': sha256(b'CPU fixture').hexdigest()})
        unit = {'unit': 'livestack-hostd.service', 'role': 'broker', 'unitSha256': 'a' * 64,
                'bootId': 'a08ce6fd-f798-4451-a054-797216ed9748', 'invocationId': 'b' * 32,
                'mainPid': 123, 'startTicks': 456, 'controlGroup': '/system.slice/livestack-hostd.service',
                'acceptedWork': 'unknown', 'impact': 'operator-aborted-unknown-result'}
        plan = {'version': 1, 'mode': 'abort', 'units': [unit], 'retainedFiles': files}
        audit = {'ok': True, 'permissionOnly': True, 'principal': 'machine:fixture', 'nonce': 'a' * 64,
                 'requestSha256': 'b' * 64, 'authorityMachine': 'fixture', 'keeperSourceSha256': 'c' * 64}
        return plan, files, audit

    def test_loaded_and_durable_mutation_refuse_before_kernel_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            plan, files, audit = self.fixture(root)
            journal = BrokerRetirementJournal(root / 'journal.sqlite', plan, files)
            journal.plan['units'][0]['mainPid'] = 777
            with patch('livestack_node.broker_retirement.inspect_predecessor') as inspect:
                with self.assertRaisesRegex(ValueError, 'loaded_plan_changed'):
                    journal.submit('livestack-hostd.service', audit)
                inspect.assert_not_called()
            journal.plan = deepcopy(plan)
            with sqlite3.connect(root / 'journal.sqlite') as db:
                db.execute("UPDATE binding SET plan=? WHERE id=1", ('f' * 64,))
            with self.assertRaisesRegex(ValueError, 'durable_binding_changed'):
                journal.status()

    def test_unknown_submitted_intent_survives_reopen_and_never_grants_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            plan, files, audit = self.fixture(root)
            path = root / 'journal.sqlite'
            journal = BrokerRetirementJournal(path, plan, files)
            # Only manager observation is synthetic. This test qualifies durable
            # intent/replay, not systemd, cgroup, CUDA, or deployment authority.
            witness = {'manager': {'InvocationID': 'b' * 32, 'MainPID': '123'}, 'processes': [{'pid': 123, 'startTicks': 456}]}
            with patch('livestack_node.broker_retirement.inspect_predecessor', return_value=witness):
                with self.assertRaises(PermissionError):
                    journal.submit('livestack-hostd.service', {'ok': True, 'permissionOnly': True})
                recorded = journal.submit('livestack-hostd.service', audit)
                self.assertFalse(recorded['authorityGranted'])
                self.assertTrue(recorded['intentRecordedOnly'])
                reopened = BrokerRetirementJournal(path, plan, files)
                self.assertEqual(reopened.status()['units'], [{'unit': 'livestack-hostd.service', 'phase': 'submitted'}])
                with self.assertRaisesRegex(ValueError, 'reconcile_only'):
                    reopened.submit('livestack-hostd.service', audit)
                self.assertFalse(reopened.status()['activationAllowed'])
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM units').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
