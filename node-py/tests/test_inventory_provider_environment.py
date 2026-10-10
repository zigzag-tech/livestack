import importlib.util
import tempfile
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('inventory_provider_environment',Path(__file__).parents[1]/'inventory_provider_environment.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class InventoryTests(unittest.TestCase):
    def test_actual_regular_bytes_internal_alias_and_external_refusal(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'runtime';root.mkdir()
            (root/'bytes').write_bytes(b'CPU fixture')
            (root/'alias').symlink_to('bytes')
            alias_root=Path(folder)/'redirect';alias_root.symlink_to(root,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'absolute_regular'):module.inventory(alias_root)
            result=module.inventory(root)
            self.assertEqual(result['files'][0]['bytes'],11)
            self.assertEqual(result['links'],[{'path':'alias','target':'bytes'}])
            (root/'alias').unlink();(root/'alias').symlink_to(Path(folder))
            with self.assertRaisesRegex(ValueError,'external'):module.inventory(root)
