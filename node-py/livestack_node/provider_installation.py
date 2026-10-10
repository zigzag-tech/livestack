"""Source-bound provider entry selected by a registered private ZZOPS app."""
import argparse
import hashlib
import importlib
import json
import os
import stat
import sys
from pathlib import Path


def sha(value):
    return hashlib.sha256(value).hexdigest()


def private_config(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid() or info.st_size > 65536:
            raise PermissionError('private_zzops_service_configuration_required')
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            value = source.read(65537)
        if len(value) > 65536:
            raise ValueError('service_configuration_bound')
        return json.loads(value), sha(value)
    finally:
        os.close(descriptor)


def read_manifest(path, digest):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('installation_manifest_path_or_bound')
    content = path.read_bytes()
    if sha(content) != digest:
        raise ValueError('installation_manifest_digest_mismatch')
    return json.loads(content)


def verify_files(root, files):
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir() or not isinstance(files, list) or not 0 < len(files) <= 65536:
        raise ValueError('installation_file_inventory_bound')
    seen = set()
    total = 0
    for record in files:
        relative = record['path']
        if not isinstance(relative,str) or relative.startswith('/') or any(p in ('', '.', '..') for p in relative.split('/')) or relative in seen:
            raise ValueError('installation_file_path')
        seen.add(relative)
        path = root / relative
        if path.resolve() != path or not path.is_file():
            raise ValueError('installation_file_symlink_or_type')
        size = path.stat().st_size
        total += size
        if size != record['bytes'] or total > 32 * 1024 ** 3:
            raise ValueError('installation_file_byte_bound')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != record['sha256']:
            raise ValueError('installation_file_digest_mismatch')


def verify_installation(config_path, app):
    config, config_digest = private_config(config_path)
    selected = [entry for entry in config.get('apps', []) if entry.get('descriptor', {}).get('app') == app]
    if len(selected) != 1:
        raise ValueError('provider_app_not_uniquely_configured')
    selected = selected[0]
    settings = selected.get('providerActivation')
    if not isinstance(settings, dict):
        raise ValueError('provider_owner_configuration_absent')
    # Registration is the existing private app descriptor and actual verified
    # adapter files, never a claimed passed/registered status in a package.
    descriptor = selected['descriptor']['adapter']
    adapter_root = Path(selected['adapterDirectory'])
    adapter = read_manifest(adapter_root/'adapter.json', descriptor['digest'])
    if adapter.get('app') != app or adapter.get('revision') != descriptor['revision']:
        raise ValueError('registered_adapter_identity_mismatch')
    verify_files(adapter_root, [{**record, 'bytes':record['size']} for record in adapter['files']])
    source = read_manifest(settings['sourceManifest'], settings['sourceManifestSha256'])
    components = source['components']
    if set(components) != {'livestack','polytts','polyasr'}:
        raise ValueError('provider_source_components_mismatch')
    canonical = json.dumps(components,sort_keys=True,separators=(',', ':')).encode()
    if sha(canonical) != source['sourceDigest']:
        raise ValueError('provider_source_identity_mismatch')
    for name, component in components.items():
        verify_files(Path(settings['sourceRoot'])/name, [{**record, 'bytes':record['sizeBytes']} for record in component['files']])
    environment = read_manifest(settings['environmentManifest'], settings['environmentManifestSha256'])
    verify_files(environment['root'], environment['files'])
    expected = {record['path'] for record in environment['files']}
    actual = set()
    links = environment.get('links', [])
    if not isinstance(links,list) or len(links)>16:
        raise ValueError('environment_link_inventory_bound')
    aliases = {}
    for record in links:
        relative, target = record['path'], record['target']
        if not isinstance(relative,str) or relative.startswith('/') or any(part in ('', '.', '..') for part in relative.split('/')) or relative in aliases or not isinstance(target,str):
            raise ValueError('environment_link_inventory_path')
        path = Path(environment['root'])/relative
        if not path.is_symlink() or os.readlink(path)!=target or not path.resolve().is_relative_to(Path(environment['root']).resolve()):
            raise ValueError('environment_internal_alias_mismatch')
        aliases[relative]=target
    observed_aliases=set()
    for directory, names, files in os.walk(environment['root'], followlinks=False):
        for name in names:
            path = Path(directory)/name
            if path.is_symlink():
                relative=str(path.relative_to(environment['root']))
                if relative not in aliases:
                    raise ValueError('environment_directory_link_not_qualified')
                observed_aliases.add(relative)
        for name in files:
            path = Path(directory)/name
            relative = str(path.relative_to(environment['root']))
            if path.is_symlink():
                if not relative.startswith('bin/python') or path.resolve() != Path(sys.executable).resolve():
                    raise ValueError('environment_external_link_not_qualified')
                continue
            actual.add(relative)
            if len(actual)>65536:
                raise ValueError('environment_inventory_count_bound')
    if observed_aliases != set(aliases):
        raise ValueError('environment_alias_inventory_mismatch')
    if actual != expected:
        raise ValueError('environment_unrecorded_or_missing_files')
    if Path(sys.prefix).resolve() != Path(environment['root']).resolve():
        raise ValueError('qualified_python_environment_root_mismatch')
    if Path(sys.executable).resolve() != Path(environment['pythonExecutable']).resolve():
        raise ValueError('qualified_python_interpreter_mismatch')
    if sha(Path(sys.executable).resolve().read_bytes()) != environment['pythonSha256']:
        raise ValueError('qualified_python_digest_mismatch')
    identity = {'app': app, 'sourceDigest': source['sourceDigest'],
                'environmentDigest': settings['environmentManifestSha256'],
                'adapterDigest': descriptor['digest'], 'configurationDigest': config_digest}
    return settings, identity



def main():
    parser = argparse.ArgumentParser(description='Verify configured provider source/environment bytes; no service effects')
    parser.add_argument('--private-service-config', required=True)
    parser.add_argument('--app', choices=('polytts','polyasr'), required=True)
    args = parser.parse_args()
    _, identity = verify_installation(args.private_service_config,args.app)
    print(json.dumps(identity,sort_keys=True))


if __name__ == '__main__':
    main()
