import importlib.util
import json
import tempfile
import unittest
from types import SimpleNamespace
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

    def test_refuses_redirected_parent_before_creating_staging_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);actual=root/'installed-cli';actual.mkdir()
            alias=root/'zzops';alias.symlink_to(actual,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'absolute_new_output'):
                module.stage(SimpleNamespace(out=str(alias/'new-stage')))
            self.assertFalse((actual/'new-stage').exists())

    def test_typing_compatibility_is_explicit_and_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);site=root/'lib/python3.12/site-packages';site.mkdir(parents=True)
            public=site/'numpy.dist-info';public.mkdir();(public/'METADATA').write_text('Name: numpy\nVersion: 2.4.4\n')
            backport=site/'typing.dist-info';backport.mkdir()
            metadata=backport/'METADATA';metadata.write_text('Name: typing\nVersion: 3.10.0.0\n')
            self.assertEqual(len(module.baseline(root)[0]),2)
            packages,excluded=module.baseline(root,True,(3,12))
            self.assertEqual(packages,[{'name':'numpy','version':'2.4.4'}])
            self.assertEqual(excluded[0]['version'],'3.10.0.0')
            with self.assertRaisesRegex(ValueError,'identity_refused'):module.baseline(root,True,(3,11))
            metadata.write_text('Name: typing\nVersion: 3.9.0.0\n')
            with self.assertRaisesRegex(ValueError,'identity_refused'):module.baseline(root,True,(3,12))
            metadata.write_text('Name: other-typing\nVersion: 3.10.0.0\n')
            with self.assertRaisesRegex(ValueError,'distribution_absent'):module.baseline(root,True,(3,12))
