"""No-effect launcher refusal controls; no installed/runtime/permission success."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from livestack_node.broker_owner_entry import protected_bytes, runtime_configuration, verify_launch, manifest_digest


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


    def test_fixed_digest_companion_shape_and_path(self):
        with patch('livestack_node.broker_owner_entry.protected_root_bytes',return_value=b'a'*64+b'\n')as read:
            self.assertEqual(manifest_digest('/fixed/launch.json','/fixed/launch.json.sha256'),'a'*64)
            read.assert_called_once_with('/fixed/launch.json.sha256',65)
        with patch('livestack_node.broker_owner_entry.protected_root_bytes')as read:
            with self.assertRaisesRegex(ValueError,'fixed_digest_companion'):manifest_digest('/fixed/launch.json','/foreign/sha256')
            read.assert_not_called()
        for value in (b'a'*64,b'A'*64+b'\n',b'a'*64+b'\r\n',b'a'*64+b'\nextra',b'g'*64+b'\n'):
            with self.subTest(value=value),patch('livestack_node.broker_owner_entry.protected_root_bytes',return_value=value):
                with self.assertRaisesRegex(ValueError,'companion_format'):manifest_digest('/fixed/launch.json','/fixed/launch.json.sha256')

    def test_wrong_companion_hash_does_not_validate_manifest(self):
        with patch('livestack_node.broker_owner_entry.protected_root_bytes',return_value=b'{}'):
            with self.assertRaisesRegex(ValueError,'root_bytes_changed'):protected_bytes('/fixed/launch.json','a'*64)
        self.assertEqual(manifest_digest('/historical/launch.json','b'*64),'b'*64)

    def test_real_user_owned_companion_refuses(self):
        with tempfile.TemporaryDirectory()as directory:
            path=Path(directory)/'launch.json';sidecar=Path(str(path)+'.sha256');sidecar.write_bytes(b'a'*64+b'\n');sidecar.chmod(0o444)
            with self.assertRaisesRegex(PermissionError,'root_ancestry'):manifest_digest(path,str(sidecar))

if __name__ == '__main__':
    unittest.main()
