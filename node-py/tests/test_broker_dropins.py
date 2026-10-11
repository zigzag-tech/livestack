"""CPU source checks; no service or physical-settlement qualification."""
from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from livestack_node.broker_predecessor import verify_registered_dropins


class RegisteredDropinsTests(unittest.TestCase):
    def test_closed_registered_set_and_bytes(self):
        unit = 'livestack-hostd.service'
        path = '/etc/systemd/system/' + unit + '.d/registered.conf'
        content = b'[Service]\nEnvironmentFile=/etc/harmony/zz-joe.env\n'
        record = {'unit': unit, 'role': 'unit', 'path': path,
                  'size': len(content), 'sha256': sha256(content).hexdigest()}
        fields = {'DropInPaths': path}
        info = SimpleNamespace(st_uid=0, st_mode=0o100644, st_size=len(content))
        with patch('pathlib.Path.lstat', return_value=info), \
             patch('pathlib.Path.is_symlink', return_value=False), \
             patch('pathlib.Path.resolve', lambda value: value), \
             patch('pathlib.Path.read_bytes', return_value=content):
            verify_registered_dropins({'unit': unit}, fields, [record])
            for actual in ('', path + ' /etc/systemd/system/' + unit + '.d/extra.conf', path + ' ' + path):
                with self.assertRaisesRegex(ValueError, 'dropins_changed'):
                    verify_registered_dropins({'unit': unit}, {'DropInPaths': actual}, [record])
            with patch('pathlib.Path.read_bytes', return_value=b'changed'):
                with self.assertRaisesRegex(ValueError, 'bytes_changed'):
                    verify_registered_dropins({'unit': unit}, fields, [record])
            with patch('pathlib.Path.lstat', return_value=SimpleNamespace(st_uid=1000, st_mode=0o100644, st_size=len(content))):
                with self.assertRaisesRegex(ValueError, 'ancestry_changed'):
                    verify_registered_dropins({'unit': unit}, fields, [record])

    def test_empty_default_preserved(self):
        verify_registered_dropins({'unit': 'polytts.service'}, {'DropInPaths': ''})
        with self.assertRaisesRegex(ValueError, 'dropins_changed'):
            verify_registered_dropins({'unit': 'polytts.service'}, {'DropInPaths': '/unregistered.conf'})


if __name__ == '__main__':
    unittest.main()
