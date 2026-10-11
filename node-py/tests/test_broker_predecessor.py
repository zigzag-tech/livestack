from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from livestack_node.broker_predecessor import validate_abort_plan, verify_retained_files, snapshot_database


class BrokerPredecessorTest(unittest.TestCase):
    def test_exact_registered_inventory_and_complete_coverage_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = []
            for role in ('source', 'unit', 'configuration', 'operations', 'ledger', 'lease-snapshot'):
                path = root / role; path.write_bytes(b'fixture')
                files.append({'path': str(path), 'role': role, 'unit': 'livestack-hostd.service',
                              'size': 7, 'sha256': sha256(b'fixture').hexdigest()})
            unit = {'unit': 'livestack-hostd.service', 'role': 'broker', 'unitSha256': 'a' * 64,
                    'bootId': 'a08ce6fd-f798-4451-a054-797216ed9748', 'invocationId': 'b' * 32,
                    'mainPid': 123, 'startTicks': 456, 'controlGroup': '/system.slice/livestack-hostd.service',
                    'acceptedWork': 'unknown', 'impact': 'operator-aborted-unknown-result'}
            plan = {'version': 1, 'mode': 'abort', 'units': [unit], 'retainedFiles': files}
            self.assertEqual(len(verify_retained_files(plan, files)), 64)
            # Regression: source-only retention cannot attest accepted history.
            with self.assertRaises(ValueError):
                validate_abort_plan({**plan, 'retainedFiles': files[:1]}, files[:1])
            for changed in ({**plan, 'retainedFiles': files[:-1]},
                            {**plan, 'retainedFiles': files + [files[0]]},
                            {**plan, 'units': [{**unit, 'unit': 'harmony-image.service'}]},
                            {**plan, 'units': [{**unit, 'impact': 'completed'}]}):
                with self.assertRaises(ValueError):
                    validate_abort_plan(changed, files)
            altered = deepcopy(files); altered[0]['sha256'] = 'f' * 64
            with self.assertRaises(ValueError):
                validate_abort_plan({**plan, 'retainedFiles': altered}, files)

    def test_consistent_wal_backup_and_final_delta_are_distinct_retained_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            original = root / 'operations.sqlite'
            connection = sqlite3.connect(original)
            try:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('CREATE TABLE accepted(id TEXT PRIMARY KEY, outcome TEXT)')
                connection.execute("INSERT INTO accepted VALUES('before','unknown')"); connection.commit()
                before = snapshot_database(original, root / 'before.sqlite')
                connection.execute("INSERT INTO accepted VALUES('later','unknown')"); connection.commit()
                after = snapshot_database(original, root / 'final.sqlite')
                self.assertNotEqual(before['sha256'], after['sha256'])
                self.assertFalse(before['finalAfterTermination'])
                with sqlite3.connect(root / 'final.sqlite') as retained:
                    self.assertEqual(retained.execute('SELECT id,outcome FROM accepted ORDER BY id').fetchall(),
                                     [('before', 'unknown'), ('later', 'unknown')])
                self.assertTrue(original.exists()); self.assertTrue(Path(str(original) + '-wal').exists())
                with self.assertRaises(ValueError):
                    snapshot_database(original, root / 'before.sqlite')
                self.assertEqual((root / 'final.sqlite').stat().st_mode & 0o777, 0o600)
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
