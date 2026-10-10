"""Actual public-wheel CPU byte test; no package import/model execution."""
import importlib.util
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

spec=importlib.util.spec_from_file_location('compatibility_resemblyzer_wheel',Path(__file__).parents[1]/'compatibility_resemblyzer_wheel.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class CompatibilityTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('RESEMBLYZER_ORIGINAL_WHEEL'),'explicit verified public artifact required')
    def test_actual_public_wheel_preserves_code_models_and_refuses_other_source_runtime(self):
        original=Path(os.environ['RESEMBLYZER_ORIGINAL_WHEEL'])
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);baseline=root/'baseline';baseline.mkdir()
            with zipfile.ZipFile(original) as wheel:wheel.extractall(baseline)
            first=module.derive(original,root/'first',(3,12),baseline)
            second=module.derive(original,root/'second',(3,12),baseline)
            self.assertEqual(first['artifactSha256'],second['artifactSha256'])
            self.assertGreater(len(first['unchangedPythonAndModelFiles']),0)
            with zipfile.ZipFile(first['artifact']) as derived:
                text=derived.read(module.NEW_INFO+'/METADATA').decode()
                self.assertIn('Version: '+module.VERSION,text)
                self.assertIn('Requires-Dist: typing; python_version < "3.5"',text)
                for record in first['unchangedPythonAndModelFiles']:
                    self.assertEqual(derived.read(record['path']),(baseline/record['path']).read_bytes())
            with self.assertRaisesRegex(ValueError,'runtime'):module.derive(original,root/'wrong-runtime',(3,11),baseline)
            broken=root/'wrong.whl';broken.write_bytes(original.read_bytes()+b'changed')
            with self.assertRaisesRegex(ValueError,'source_digest'):module.derive(broken,root/'wrong-source',(3,12),baseline)
            code=baseline/first['unchangedPythonAndModelFiles'][0]['path'];code.write_bytes(b'changed retained code')
            with self.assertRaisesRegex(ValueError,'baseline_bytes'):module.derive(original,root/'changed-code',(3,12),baseline)
