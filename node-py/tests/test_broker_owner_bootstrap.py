import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from types import SimpleNamespace

from livestack_node.broker_owner_bootstrap import configure_broker_owner


class BrokerOwnerBootstrapTest(unittest.TestCase):
    def test_private_same_socket_starts_held_and_refuses_source_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            config = root / 'owner.json'
            identity = {'sourceSha256': 'a' * 64}
            value = {'version': 1, 'controlSocket': str(root / 'owner.sock'),
                     'journalPath': str(root / 'journal.sqlite'), 'sourceIdentity': identity,
                     'startupHolder': 'fixture-held'}
            config.write_text(json.dumps(value)); config.chmod(0o600)
            broker = SimpleNamespace(fleet_pools=[], _in_flight={})
            gate, control = configure_broker_owner(broker, str(config), identity, 'fixture-incarnation')
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                    peer.settimeout(2); peer.connect(str(root / 'owner.sock'))
                    peer.sendall(b'{"operation":"status"}\n')
                    result = json.loads(peer.recv(8192))
                self.assertIs(result['ok'], True)
                self.assertEqual(result['result']['holder'], 'fixture-held')
                self.assertIs(result['result']['drained'], True)
                self.assertEqual(result['result']['serverProcessId'], os.getpid())
                self.assertEqual(result['result']['sourceIdentity'], identity)
                with self.assertRaisesRegex(Exception, 'provider_admission_held'):
                    gate.fence.admit()
            finally:
                control.close()
            for bad in ({**value, 'permission': True}, {**value, 'sourceIdentity': {'sourceSha256': 'b' * 64}}):
                config.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    configure_broker_owner(broker, str(config), identity, 'fixture-incarnation')
                self.assertFalse((root / 'owner.sock').exists())


if __name__ == '__main__':
    unittest.main()
