"""No-effect launcher refusal controls; no installed/runtime/permission success."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from livestack_node.broker_owner_entry import protected_bytes, runtime_configuration, verify_launch


class BrokerOwnerEntryTests(unittest.TestCase):
    def test_real_user_owned_file_and_alias_cannot_supply_protected_source(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'source'
            file.write_bytes(b'known')
            file.chmod(0o444)
            expected = hashlib.sha256(b'known').hexdigest()
            with self.assertRaisesRegex(PermissionError, 'root_ancestry'):
                protected_bytes(file, expected)
            alias = file.with_name('alias'); alias.symlink_to(file)
            with self.assertRaisesRegex(PermissionError, 'root_alias'):
                protected_bytes(alias, expected)

    def test_manifest_and_escaping_source_refuse_before_loading_verifier(self):
        with self.assertRaisesRegex(ValueError, 'manifest_digest'):
            verify_launch('/unused', '')
        with patch('livestack_node.broker_owner_entry.protected_bytes', return_value=b'{}'):
            with self.assertRaisesRegex(ValueError, 'closed_manifest'):
                verify_launch('/unused', 'a' * 64)
        value = dict.fromkeys(('version', 'sourceRoot', 'sourceFiles', 'sourceSha256', 'sealVerifier',
            'sealedRuntime', 'runtimeConfiguration', 'privateInputs', 'unitSha256'))
        value.update(version=1, sourceRoot='/usr', sourceFiles=[{'path':'../escape','sha256':'a'*64,'bytes':1}])
        with patch('livestack_node.broker_owner_entry.protected_bytes', return_value=json.dumps(value).encode()), \
                patch('livestack_node.broker_owner_entry.importlib.util.spec_from_file_location') as loader:
            with self.assertRaisesRegex(ValueError, 'source_path'):
                verify_launch('/unused', 'a' * 64)
            loader.assert_not_called()

    def test_private_mutable_configuration_refuses_even_matching_sha(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder)/'private.json'; file.write_bytes(b'{}'); file.chmod(0o600)
            class NoSeal:
                IMMUTABLE = 0x10
                @staticmethod
                def flags(path): return 0
            with self.assertRaisesRegex(PermissionError, 'immutable_private'):
                runtime_configuration({'path':str(file),'sha256':hashlib.sha256(b'{}').hexdigest()},NoSeal)


if __name__ == '__main__':
    unittest.main()
