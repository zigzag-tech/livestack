"""Actual pinned bundle/Node controls; no real network grant, lease or model."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from livestack_node.provider_installation import sha
from livestack_node.provider_qualification import QualificationClient, qualification_source_digest


@unittest.skipUnless(os.environ.get('ZZOPS_QUALIFICATION_BUNDLE') and os.environ.get('ZZOPS_QUALIFICATION_SOURCE'), 'explicit source-qualified Node bundle fixture required')
class QualificationClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        bundle = Path(os.environ['ZZOPS_QUALIFICATION_BUNDLE'])
        self.bundle = self.root / 'bundle'; shutil.copytree(bundle, self.bundle)
        manifest = json.loads((self.bundle / 'manifest.json').read_bytes())
        self.inputs = self.root / 'inputs'; self.inputs.mkdir()
        for record in manifest['sourceInputs']:
            target = self.inputs / record['path']; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(os.environ['ZZOPS_QUALIFICATION_SOURCE']) / record['path'], target)
        self.service_config = self.root / 'service.json'; self.service_config.write_text('{"fixture":true}'); self.service_config.chmod(0o600)
        self.identity = {'app': 'polytts', 'sourceDigest': 'a'*64, 'environmentDigest': 'b'*64,
                         'adapterDigest': 'c'*64, 'configurationDigest': sha(self.service_config.read_bytes())}
        self.manifest_sha = sha((self.bundle / 'manifest.json').read_bytes())
        source = qualification_source_digest(self.identity, self.manifest_sha)
        for name in ('fixture-key', 'fixture-known-hosts'):
            path = self.root / name; path.write_text('synthetic, never used on network'); path.chmod(0o600)
        config = {'principal': 'machine:fixture', 'token': 'zzops_'+'e'*64,
            'ssh': {'destination': 'fixture@127.0.0.1', 'expectedAuthority': 'fixture-authority',
                'identity_file': str(self.root/'fixture-key'), 'known_hosts_file': str(self.root/'fixture-known-hosts'),
                'keeperSourceSha256': 'f'*64},
            'intent': {'app': 'polytts', 'adapterDigest': self.identity['adapterDigest'], 'adapterRevision': 'a'*40,
                'candidate': 'b'*64, 'sourceIdentitySha256': source, 'target': 'fixture-joe',
                'scope': {'mode': 'qualification-only', 'service': 'polytts-qualification.service',
                          'controlGroup': '/system.slice/polytts-qualification.service', 'port': 18100, 'gpuIndex': '1'}}}
        self.config_path = self.root / 'private.json'; self.config_path.write_text(json.dumps(config)); self.config_path.chmod(0o600)
        self.settings = {'manifestPath': str(self.bundle/'manifest.json'), 'manifestSha256': self.manifest_sha,
            'inputRoot': str(self.inputs), 'nodeExecutable': manifest['toolchain']['node']['path'],
            'nodeSha256': manifest['toolchain']['node']['sha256'], 'configurationPath': str(self.config_path),
            'configurationSha256': sha(self.config_path.read_bytes()), 'serviceConfigurationPath': str(self.service_config)}

    def test_actual_constructor_and_anonymous_forged_or_job_control_caller_refuse(self):
        client = QualificationClient(self.settings, self.identity)
        for authorization in (None, '', 'Bearer job-control-token', 'Bearer zzops_'+'0'*64):
            with self.assertRaisesRegex(PermissionError, 'input_refused'):
                client.authorize(effect='model-submit', job_id=str(uuid4()), owner_id='fixture-owner',
                    input_digest='e'*64, model_kind='voxcpm', authorization=authorization)

    def test_changed_bundle_source_configuration_or_node_pin_refuse(self):
        client = QualificationClient(self.settings, self.identity)
        bytes = self.config_path.read_bytes(); self.config_path.write_bytes(bytes+b' ')
        with self.assertRaisesRegex(ValueError, 'configuration_changed'): client._verify()
        self.config_path.write_bytes(bytes)
        output = self.bundle/'client.mjs'; bytes = output.read_bytes(); output.write_bytes(bytes+b'// altered\n')
        with self.assertRaises(ValueError): client._verify()
        output.write_bytes(bytes)
        input_file = next(self.inputs.rglob('provider-qualification-client.mjs')); retained = input_file.read_bytes(); input_file.write_text('changed source')
        with self.assertRaises(ValueError): client._verify()
        input_file.write_bytes(retained)
        with self.assertRaisesRegex(ValueError, 'node_executable_identity'): QualificationClient({**self.settings, 'nodeSha256': '0'*64}, self.identity)
        self.service_config.write_text('{"fixture":false}')
        with self.assertRaisesRegex(ValueError, 'service_configuration_changed'): client._verify()

if __name__ == '__main__': unittest.main()
