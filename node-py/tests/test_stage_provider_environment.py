import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('stage_provider_environment',Path(__file__).parents[1]/'stage_provider_environment.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class BaselineTests(unittest.TestCase):
    def test_exact_public_versions_and_owner_editables_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);site=root/'lib/python3.12/site-packages';site.mkdir(parents=True)
            for name in ('numpy','livestack-node','shared-py'):
                info=site/(name+'.dist-info');info.mkdir();(info/'METADATA').write_text('Name: '+name+'\nVersion: 1.2.3\n')
                if name=='livestack-node':(info/'direct_url.json').write_text(json.dumps({'dir_info':{'editable':True}}))
            packages,excluded=module.baseline(root)
            self.assertEqual(packages,[{'name':'numpy','version':'1.2.3'}])
            self.assertEqual({item['name'] for item in excluded},{'livestack-node','shared-py'})
            info=site/'unowned.dist-info';info.mkdir();(info/'METADATA').write_text('Name: unowned\nVersion: 1.0\n')
            (info/'direct_url.json').write_text(json.dumps({'dir_info':{'editable':True}}))
            with self.assertRaisesRegex(ValueError,'non_owner_editable'):module.baseline(root)
