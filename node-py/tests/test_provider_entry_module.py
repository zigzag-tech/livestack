"""Byte/path entry controls only; no server, backend, model or unit effects."""
import tempfile
import unittest
from pathlib import Path
from livestack_node.provider_entry import verify_entry_module


class EntryModuleTests(unittest.TestCase):
    def test_source_and_exact_installed_copy_preserve_isolated_entry(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);source=root/'source/livestack/node-py/livestack_node/provider_entry.py'
            source.parent.mkdir(parents=True);source.write_bytes(b'CPU entry bytes')
            environment=root/'venv';installed=environment/'lib/python3.12/site-packages/livestack_node/provider_entry.py'
            installed.parent.mkdir(parents=True);installed.write_bytes(source.read_bytes())
            verify_entry_module(root/'source',environment,source)
            verify_entry_module(root/'source',environment,installed)
            foreign=root/'foreign/livestack_node/provider_entry.py';foreign.parent.mkdir(parents=True);foreign.write_bytes(source.read_bytes())
            with self.assertRaisesRegex(ValueError,'path_or_bytes_mismatch'):
                verify_entry_module(root/'source',environment,foreign)
            installed.write_bytes(b'changed entry')
            with self.assertRaisesRegex(ValueError,'path_or_bytes_mismatch'):
                verify_entry_module(root/'source',environment,installed)
            installed.unlink();installed.symlink_to(foreign)
            with self.assertRaisesRegex(ValueError,'path_or_bytes_mismatch'):
                verify_entry_module(root/'source',environment,installed)
