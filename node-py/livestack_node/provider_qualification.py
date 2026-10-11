"""Source-bound canonical input permission; deliberately not a resource lease."""
import json
import subprocess
from pathlib import Path

from .provider_installation import private_config, read_manifest, sha, verify_files


def qualification_source_digest(identity, authorization_manifest_digest):
    source = {key: identity[key] for key in ('app', 'sourceDigest', 'environmentDigest', 'adapterDigest')}
    source['authorizationManifestDigest'] = authorization_manifest_digest
    return sha(json.dumps(source, sort_keys=True, separators=(',', ':')).encode())


class QualificationClient:
    """The fixed bundle verifies caller possession, then fresh canonical grants.

    Constructor/source checks alone are not kernel custody, node registration,
    host capacity reservation or an active kind residency lease. A model caller
    must separately obtain all of those before invoking a loader.
    """
    def __init__(self, settings, identity):
        fields = {'manifestPath', 'manifestSha256', 'inputRoot', 'nodeExecutable', 'nodeSha256',
                  'configurationPath', 'configurationSha256', 'serviceConfigurationPath'}
        if not isinstance(settings, dict) or set(settings) != fields:
            raise ValueError('closed_qualification_client_settings_required')
        self.settings = dict(settings)
        self.identity = dict(identity)
        self.source_digest = qualification_source_digest(identity, settings['manifestSha256'])
        self._verify()

    def _verify(self):
        settings = self.settings
        _, service_digest = private_config(settings['serviceConfigurationPath'])
        if service_digest != self.identity['configurationDigest']:
            raise ValueError('qualification_service_configuration_changed')
        manifest = read_manifest(settings['manifestPath'], settings['manifestSha256'])
        if manifest.get('version') != 1 or manifest.get('kind') != 'provider-qualification-client-bundle' or manifest.get('authorityRegistration') is not False or manifest.get('resourceLease') is not False:
            raise ValueError('qualification_bundle_identity_invalid')
        root = Path(settings['manifestPath']).parent
        outputs = manifest['outputs']
        if not isinstance(outputs, list) or {record.get('path') for record in outputs} != {'client.mjs', 'client-library.mjs'} or len(outputs) != 2:
            raise ValueError('qualification_bundle_outputs_required')
        for record in outputs:
            if not isinstance(record.get('builtinImports'), list) or any(item not in ('node:child_process', 'node:crypto', 'node:fs', 'node:path') for item in record['builtinImports']):
                raise ValueError('qualification_bundle_external_dependency')
        verify_files(root, [{**record, 'bytes': record['sizeBytes']} for record in outputs])
        verify_files(settings['inputRoot'], [{**record, 'bytes': record['sizeBytes']} for record in manifest['sourceInputs']])
        node = Path(settings['nodeExecutable'])
        if not node.is_absolute() or node.resolve() != node or not node.is_file() or sha(node.read_bytes()) != settings['nodeSha256'] or settings['nodeSha256'] != manifest['toolchain']['node']['sha256']:
            raise ValueError('qualification_node_executable_identity_changed')
        for record in manifest['toolchain']['nodeRuntimeLibraries']:
            path = Path(record['path'])
            if not path.is_absolute() or path.resolve() != path or not path.is_file() or path.stat().st_size != record['sizeBytes'] or sha(path.read_bytes()) != record['sha256']:
                raise ValueError('qualification_node_runtime_library_changed')
        environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        probe = subprocess.run([str(node), '-p', 'JSON.stringify({versions:process.versions,platform:process.platform,arch:process.arch,shared:process.report.getReport().sharedObjects})'],
                               env=environment, capture_output=True, timeout=20, check=True)
        if len(probe.stdout) > 16384:
            raise ValueError('qualification_node_probe_bound')
        actual = json.loads(probe.stdout)
        toolchain = manifest['toolchain']
        actual_libraries = {str(Path(path).resolve()) for path in actual['shared'] if Path(path).is_absolute()}
        if actual['versions'] != toolchain['nodeVersions'] or actual['platform'] != toolchain['platform'] or actual['arch'] != toolchain['arch'] or actual_libraries != {record['path'] for record in toolchain['nodeRuntimeLibraries']} or [path for path in actual['shared'] if not Path(path).is_absolute()] != toolchain['kernelRuntimeObjects']:
            raise ValueError('qualification_node_runtime_abi_changed')
        config, digest = private_config(settings['configurationPath'])
        if digest != settings['configurationSha256'] or set(config) != {'intent', 'principal', 'ssh', 'token'}:
            raise ValueError('qualification_authority_configuration_changed')
        intent = config['intent']
        if intent.get('app') != self.identity['app'] or intent.get('adapterDigest') != self.identity['adapterDigest'] or intent.get('sourceIdentitySha256') != self.source_digest:
            raise ValueError('qualification_authority_source_binding_changed')
        return root / 'client.mjs', config

    def authorize(self, *, effect, job_id, owner_id, input_digest, model_kind, authorization):
        if effect not in ('model-submit', 'residency'):
            raise ValueError('qualification_model_effect_required')
        executable, config = self._verify()
        model = {'effect': effect, 'jobId': job_id, 'ownerId': owner_id, 'inputDigest': input_digest, 'modelKind': model_kind}
        process = subprocess.run([self.settings['nodeExecutable'], str(executable), self.settings['configurationPath']],
            input=json.dumps({'authorization': authorization, 'input': model}).encode(), env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
            capture_output=True, timeout=20)
        if process.returncode != 0 or len(process.stdout) > 16384:
            raise PermissionError('canonical_qualification_input_refused')
        receipt = json.loads(process.stdout)
        expected = {**config['intent'], **model}
        if not isinstance(receipt, dict) or set(receipt) != set(expected) | {'receipt'} or any(receipt.get(key) != value for key, value in expected.items()) or not isinstance(receipt['receipt'], dict) or receipt['receipt'].get('ok') is not True or receipt['receipt'].get('principal') != config['principal']:
            raise PermissionError('canonical_qualification_receipt_binding_refused')
        return receipt
