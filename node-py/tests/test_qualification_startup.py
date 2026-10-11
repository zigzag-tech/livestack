"""Real startup/SQLite wiring; authorization client and GPU peer are CPU mocks."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from livestack_node.hostbroker import HostBroker
from livestack_node.hostd import configured_broker_app
from livestack_node.planner import Device
from livestack_node.provider_installation import sha


class QualificationStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.broker = HostBroker([Device('gpu', 'host', {'vram_bytes': 6})], [])
        self.database = self.root / 'claims.sqlite'
        self.config = self.root / 'qualification.json'

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_configuration_preserves_original_app(self):
        sentinel = object()
        with patch('livestack_node.hostd.build_app', return_value=sentinel) as build:
            self.assertIs(configured_broker_app(self.broker), sentinel)
            build.assert_called_once_with(self.broker)
        self.assertIsNone(self.broker.qualification_reservations)
        self.assertFalse(self.database.exists())

    def test_partial_noncanonical_and_invalid_configuration_never_adopts_writer(self):
        for config, database in [(str(self.config), ''), ('', str(self.database)),
                                 ('relative.json', str(self.database))]:
            with self.assertRaises(ValueError):
                configured_broker_app(self.broker, config, database)
        self.config.write_text(json.dumps({'version': 2, 'nodes': []}))
        self.config.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'closed_qualification_controller'):
            configured_broker_app(self.broker, str(self.config), str(self.database))
        self.assertFalse(self.database.exists())
        self.assertIsNone(self.broker.qualification_reservations)

    def test_valid_sources_attach_real_durable_store_and_route_without_admission(self):
        caller = self.root / 'caller.json'
        caller.write_text(json.dumps({'intent': {'scope': {'service': 'polytts-qualification.service'},
                                                'targetMachine': 'host'},
                                      'ssh': {'expectedAuthority': 'auth'}}))
        caller.chmod(0o600)
        node = dict(nodeId='node', authority='auth', identitySha256='a'*64,
                    configurationSha256='b'*64, serverInstanceId='server', bootId='boot',
                    unitInvocationId='unit', processStartTicks='1', deviceId='gpu',
                    unitKind='qualified', hostId='host')
        entry = {'binding': node, 'sourceIdentity': {'configurationDigest': 'b'*64},
                 'unit': {'kind': 'qualified', 'footprint': {'vram_bytes': 4}},
                 'ownerSocket': str(self.root/'owner.sock'), 'unitSha256': 'c'*64,
                 'service': 'polytts-qualification.service',
                 'clientSettings': {'configurationPath': str(caller),
                                    'configurationSha256': sha(caller.read_bytes())}}
        self.config.write_text(json.dumps({'version': 1, 'nodes': [entry]}))
        self.config.chmod(0o600)
        client = Mock(source_digest='a'*64)
        with patch('livestack_node.qualification_controller.QualificationClient', return_value=client):
            app = configured_broker_app(self.broker, str(self.config), str(self.database))
        self.assertTrue(self.database.exists())
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)
        self.assertIn('/qualification/admit', {route.path for route in app.routes})
        self.assertEqual(self.broker.snapshot().devices[0].reserved, {})
        client.authorize.assert_not_called()

    def test_wrong_registered_source_refuses_before_database_creation(self):
        self.config.write_text(json.dumps({'version': 1, 'nodes': [{'binding': {}}]}))
        self.config.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'closed_registered_qualification_node'):
            configured_broker_app(self.broker, str(self.config), str(self.database))
        self.assertFalse(self.database.exists())


if __name__ == '__main__':
    unittest.main()
