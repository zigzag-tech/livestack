"""Real same-socket/ASGI CPU startup; no units, models or registered authority."""
import json
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from livestack_node.broker_owner_bootstrap import configured_owned_broker_app
from livestack_node.hostbroker import HostBroker
from livestack_node.hostd import main


class BrokerOwnerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_owner_starts_held_on_the_actual_broker_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            identity = {'sourceSha256': 'a' * 64}
            config = root / 'owner.json'
            config.write_text(json.dumps({'version': 1, 'controlSocket': str(root / 'owner.sock'),
                'journalPath': str(root / 'effects.sqlite'), 'sourceIdentity': identity,
                'startupHolder': 'fixture-held'}))
            config.chmod(0o600)
            with patch.dict(os.environ, {'LIVESTACK_REPLAN_INTERVAL': '0'}):
                app, control = configured_owned_broker_app(HostBroker([], []), '', '',
                    str(config), identity, 'fixture-boot:1:10')
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture.invalid') as client:
                    self.assertEqual((await client.post('/admit', json={'kind': 'voxcpm'})).status_code, 503)
                    self.assertEqual((await client.get('/status')).status_code, 200)
                self.assertTrue((root / 'owner.sock').exists())
            finally:
                control.close()

    async def test_failed_paired_qualification_cleans_up_same_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            identity = {'sourceSha256': 'a' * 64}
            config = root / 'owner.json'
            config.write_text(json.dumps({'version': 1, 'controlSocket': str(root / 'owner.sock'),
                'journalPath': str(root / 'effects.sqlite'), 'sourceIdentity': identity,
                'startupHolder': 'fixture-held'})); config.chmod(0o600)
            with self.assertRaisesRegex(ValueError, 'paired_qualification'):
                configured_owned_broker_app(HostBroker([], []), '/missing', '', str(config), identity, 'fixture')
            self.assertFalse((root / 'owner.sock').exists())

    async def test_recovery_exception_closes_actual_owner_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            identity = {'sourceSha256': 'a' * 64}
            config = root / 'owner.json'
            config.write_text(json.dumps({'version': 1, 'controlSocket': str(root / 'owner.sock'),
                'journalPath': str(root / 'effects.sqlite'), 'sourceIdentity': identity,
                'startupHolder': 'fixture-held'})); config.chmod(0o600)
            store = SimpleNamespace(path='CPU fixture', max_records=10, max_age_s=None)
            with patch.dict(os.environ, {'LIVESTACK_PEERS': 'none', 'LIVESTACK_LEDGER': '0',
                    'LIVESTACK_REPLAN_INTERVAL': '0'}, clear=True), \
                    patch('livestack_node.fleet_operations.store_from_env', return_value=store), \
                    patch('livestack_node.fleet_workers.recover_all', side_effect=RuntimeError('CPU recovery failure')) as recovery, \
                    patch('uvicorn.run') as serve, redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'CPU recovery failure'):
                    main(owner_configuration=str(config), owner_source_identity=identity, owner_incarnation='fixture')
                recovery.assert_called_once()
                serve.assert_not_called()
            self.assertFalse((root / 'owner.sock').exists())
            self.assertTrue((root / 'effects.sqlite').exists())

    async def test_absent_defaults_and_incomplete_owner_tuples(self):
        broker = object()
        with patch('livestack_node.hostd.configured_broker_app', return_value='original') as original:
            self.assertEqual(configured_owned_broker_app(broker, '', '', '', None, None), ('original', None))
            original.assert_called_once_with(broker, '', '')
        for values in ({'owner_configuration': '/private/owner.json'},
                       {'owner_source_identity': {'sourceSha256': 'a' * 64}},
                       {'owner_incarnation': 'fixture'}):
            with self.assertRaisesRegex(ValueError, 'paired_broker_owner_startup'):
                main(**values)


if __name__ == '__main__':
    unittest.main()
