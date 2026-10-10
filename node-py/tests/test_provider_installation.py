import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from livestack_node.provider_installation import verify_installation


def digest(value):return hashlib.sha256(value).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',', ':')).encode()


class InstallationTests(unittest.TestCase):
    def test_actual_configured_source_bytes_environment_manifest_and_negative_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root/'adapter';adapter.mkdir()
            code = b'export default {};';(adapter/'policy.mjs').write_bytes(code)
            manifest = {'app':'polytts','revision':'a'*40,'files':[{'path':'policy.mjs','size':len(code),'sha256':digest(code)}]}
            payload = canonical(manifest);(adapter/'adapter.json').write_bytes(payload)
            components = {}
            for name in ('polytts','polyasr','livestack'):
                target = root/'source'/name;target.mkdir(parents=True)
                (target/'fixture.py').write_bytes(b'# honest synthetic source')
                content = (target/'fixture.py').read_bytes()
                components[name] = {'revision':'b'*40,'files':[{'path':'fixture.py','sizeBytes':len(content),'sha256':digest(content)}]}
            source = {'components':components,'sourceDigest':digest(canonical(components))}
            source_path = root/'source.json';source_path.write_bytes(canonical(source))
            environment_root = root/'environment';environment_root.mkdir()
            (environment_root/'lib').mkdir()
            (environment_root/'lib64').symlink_to('lib',target_is_directory=True)
            (environment_root/'fixture.txt').write_bytes(b'fixture dependency inventory; not real qualification')
            content = (environment_root/'fixture.txt').read_bytes()
            environment = {'links':[{'path':'lib64','target':'lib'}],'root':str(environment_root),'pythonExecutable':sys.executable,'pythonSha256':digest(Path(sys.executable).resolve().read_bytes()),
                           'files':[{'path':'fixture.txt','bytes':len(content),'sha256':digest(content)}]}
            runtime_root=root/'python-runtime';runtime_root.mkdir()
            (runtime_root/'stdlib.py').write_bytes(b'CPU runtime fixture')
            (runtime_root/'stdlib-alias.py').symlink_to('stdlib.py')
            environment['runtimeTree']={'root':str(runtime_root),'links':[{'path':'stdlib-alias.py','target':'stdlib.py'}],'files':[{'path':'stdlib.py','bytes':19,'sha256':digest(b'CPU runtime fixture')}]}
            base_patch=patch.object(sys,'base_prefix',str(runtime_root));base_patch.start();self.addCleanup(base_patch.stop)
            environment_path = root/'environment.json';environment_path.write_bytes(canonical(environment))
            config = {'apps':[{'descriptor':{'app':'polytts','adapter':{'revision':'a'*40,'digest':digest(payload)}},
                'adapterDirectory':str(adapter),'providerActivation':{'sourceRoot':str(root/'source'),
                'sourceManifest':str(source_path),'sourceManifestSha256':digest(source_path.read_bytes()),
                'environmentManifest':str(environment_path),'environmentManifestSha256':digest(environment_path.read_bytes())}}]}
            config_path = root/'private-service.json';config_path.write_bytes(canonical(config));config_path.chmod(0o600)
            prefix_patch=patch.object(sys,'prefix',str(environment_root));prefix_patch.start();self.addCleanup(prefix_patch.stop)
            _, identity = verify_installation(config_path,'polytts')
            self.assertEqual(identity['sourceDigest'],source['sourceDigest'])
            (runtime_root/'stdlib-alias.py').unlink();(runtime_root/'stdlib-alias.py').symlink_to(root/'source/polytts/fixture.py')
            with self.assertRaisesRegex(ValueError,'runtime_internal_alias'):verify_installation(config_path,'polytts')
            (runtime_root/'stdlib-alias.py').unlink();(runtime_root/'stdlib-alias.py').symlink_to('stdlib.py')
            (runtime_root/'unexpected.py').write_text('unrecorded runtime')
            with self.assertRaisesRegex(ValueError,'runtime_unrecorded'):verify_installation(config_path,'polytts')
            (runtime_root/'unexpected.py').unlink()
            (runtime_root/'stdlib.py').write_bytes(b'tampered runtime')
            with self.assertRaises(ValueError):verify_installation(config_path,'polytts')
            (runtime_root/'stdlib.py').write_bytes(b'CPU runtime fixture')
            with patch.object(sys,'base_prefix','/wrong-runtime'):
                with self.assertRaisesRegex(ValueError,'runtime_root'):verify_installation(config_path,'polytts')
            (environment_root/'lib64').unlink();(environment_root/'lib64').symlink_to(root,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'alias_mismatch'):verify_installation(config_path,'polytts')
            (environment_root/'lib64').unlink();(environment_root/'lib64').symlink_to('lib',target_is_directory=True)
            (environment_root/'unrecorded.py').write_text('unrecorded')
            with self.assertRaisesRegex(ValueError,'unrecorded'):verify_installation(config_path,'polytts')
            (environment_root/'unrecorded.py').unlink()
            with patch.object(sys,'prefix','/wrong-environment'):
                with self.assertRaisesRegex(ValueError,'environment_root'):verify_installation(config_path,'polytts')
            with self.assertRaisesRegex(ValueError,'not_uniquely'):verify_installation(config_path,'polyasr')
            (root/'source/polytts/fixture.py').write_bytes(b'changed')
            with self.assertRaises(ValueError):verify_installation(config_path,'polytts')
            (root/'source/polytts/fixture.py').write_bytes(b'# honest synthetic source')
            (adapter/'policy.mjs').write_bytes(b'changed adapter')
            with self.assertRaises(ValueError):verify_installation(config_path,'polytts')
            (adapter/'policy.mjs').write_bytes(code)
            (environment_root/'fixture.txt').unlink()
            with self.assertRaises(ValueError):verify_installation(config_path,'polytts')
            config_path.chmod(0o644)
            with self.assertRaises(PermissionError):verify_installation(config_path,'polytts')
