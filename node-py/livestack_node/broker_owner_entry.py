"""Source-bound broker launcher. Installation and canonical permission are separate."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


def digest(value):
    return hashlib.sha256(value).hexdigest()


def protected_bytes(path, expected, limit=65536):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise PermissionError('broker_entry_root_alias_refused')
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError('broker_entry_root_ancestry_required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_mode & 0o022 or before.st_nlink != 1 or before.st_size > limit:
            raise PermissionError('broker_entry_root_file_required')
        value = os.read(fd, limit + 1)
        after = path.lstat()
        if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size) or digest(value) != expected:
            raise ValueError('broker_entry_root_bytes_changed')
        return value
    finally:
        os.close(fd)


def immutable_private_bytes(record, seal):
    path = Path(record['path'])
    if path.resolve() != path:
        raise PermissionError('broker_entry_private_alias_refused')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 65536 or seal.flags(path) & seal.IMMUTABLE != seal.IMMUTABLE:
            raise PermissionError('broker_entry_immutable_private_configuration_required')
        data = os.read(fd, 65537)
        after = path.lstat()
        if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size) or digest(data) != record['sha256']:
            raise ValueError('broker_entry_private_bytes_changed')
        return data
    finally:
        os.close(fd)


def runtime_configuration(record, seal):
    return json.loads(immutable_private_bytes(record, seal))


def verify_launch(path, expected):
    if not re.fullmatch('[a-f0-9]{64}', expected):
        raise ValueError('broker_entry_manifest_digest_required')
    value = json.loads(protected_bytes(path, expected))
    fields = {'version', 'sourceRoot', 'sourceFiles', 'sourceSha256', 'sealVerifier', 'sealedRuntime', 'runtimeConfiguration', 'privateInputs', 'unitSha256'}
    if not isinstance(value, dict) or set(value) != fields or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('broker_entry_closed_manifest_required')
    root = Path(value['sourceRoot'])
    if not root.is_absolute() or root.resolve() != root or not isinstance(value['sourceFiles'], list) or not 1 <= len(value['sourceFiles']) <= 1024:
        raise ValueError('broker_entry_closed_source_required')
    seen = set()
    for row in value['sourceFiles']:
        if not isinstance(row, dict) or set(row) != {'path', 'sha256', 'bytes'}:
            raise ValueError('broker_entry_closed_source_file_required')
        relative = Path(row['path'])
        if relative.is_absolute() or '..' in relative.parts or row['path'] in seen:
            raise ValueError('broker_entry_source_path_refused')
        seen.add(row['path'])
        data = protected_bytes(root / relative, row['sha256'], 1048576)
        if len(data) != row['bytes']:
            raise ValueError('broker_entry_source_size_changed')
    actual = {str(file.relative_to(root)) for file in root.rglob('*') if not file.is_dir()}
    if actual != seen or digest(json.dumps(value['sourceFiles'], sort_keys=True, separators=(',', ':')).encode()) != value['sourceSha256']:
        raise ValueError('broker_entry_source_membership_changed')
    if Path(__file__).resolve() != root / 'livestack_node/broker_owner_entry.py':
        raise PermissionError('broker_entry_registered_entry_required')
    row = value['sealVerifier']
    if not isinstance(row, dict) or set(row) != {'path', 'sha256'}:
        raise ValueError('broker_entry_closed_seal_verifier_required')
    protected_bytes(row['path'], row['sha256'], 65536)
    spec = importlib.util.spec_from_file_location('registered_broker_runtime_seal', row['path'])
    seal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seal)
    runtime = value['sealedRuntime']
    if not isinstance(runtime, dict) or set(runtime) != {'configuration', 'record', 'recordSha256', 'environmentManifest', 'environmentManifestSha256', 'python', 'pythonSha256'}:
        raise ValueError('broker_entry_closed_runtime_required')
    seal.verify(Path(runtime['configuration']), 'polytts', Path(runtime['record']))
    if digest(Path(runtime['record']).read_bytes()) != runtime['recordSha256']:
        raise ValueError('broker_entry_exact_seal_record_changed')
    # These bytes are reused as a Python toolchain, never as broker permission.
    environment = json.loads(Path(runtime['environmentManifest']).read_bytes())
    if digest(Path(runtime['environmentManifest']).read_bytes()) != runtime['environmentManifestSha256'] or environment['pythonExecutable'] != runtime['python'] or environment['pythonSha256'] != runtime['pythonSha256']:
        raise ValueError('broker_entry_exact_runtime_changed')
    if not isinstance(value['runtimeConfiguration'], dict) or set(value['runtimeConfiguration']) != {'path', 'sha256'} or not isinstance(value['privateInputs'], list) or not 2 <= len(value['privateInputs']) <= 16:
        raise ValueError('broker_entry_closed_private_inputs_required')
    selected = runtime_configuration(value['runtimeConfiguration'], seal)
    if not isinstance(selected, dict) or set(selected) != {'environment', 'ownerConfiguration', 'qualificationConfiguration', 'reservationPath'}:
        raise ValueError('broker_entry_closed_runtime_configuration_required')
    for row in value['privateInputs']:
        if not isinstance(row, dict) or set(row) != {'path', 'sha256'}:
            raise ValueError('broker_entry_closed_private_input_required')
        immutable_private_bytes(row, seal)
    if not isinstance(selected['environment'], dict):
        raise ValueError('broker_entry_explicit_environment_required')
    for key, val in selected['environment'].items():
        if not isinstance(key, str) or not key.startswith('LIVESTACK_') or key == 'LIVESTACK_FLEET_TOKENS' or not isinstance(val, str):
            raise ValueError('broker_entry_explicit_environment_required')
    required = {selected['ownerConfiguration'], selected['qualificationConfiguration']}
    if not required.issubset({row['path'] for row in value['privateInputs']}):
        raise ValueError('broker_entry_bound_private_inputs_required')
    protected_bytes('/etc/systemd/system/livestack-hostd.service', value['unitSha256'])
    return value, selected


def main():
    if len(sys.argv) != 3:
        raise ValueError('broker_entry_fixed_manifest_arguments_required')
    value, selected = verify_launch(sys.argv[1], sys.argv[2])
    runtime = value['sealedRuntime']
    if Path('/proc/self/exe').resolve() != Path(runtime['python']).resolve():
        os.execve(runtime['python'], [runtime['python'], '-I', '-B', __file__, *sys.argv[1:]], {'PATH': '/usr/bin:/bin'})
    observed = subprocess.run(['/usr/bin/systemctl', 'show', 'livestack-hostd.service', '--property=MainPID,InvocationID,ControlGroup,FragmentPath'], capture_output=True, text=True, check=True, timeout=20)
    fields = dict(line.split('=', 1) for line in observed.stdout.splitlines() if '=' in line)
    if int(fields['MainPID']) != os.getpid() or fields['FragmentPath'] != '/etc/systemd/system/livestack-hostd.service' or fields['ControlGroup'] != '/system.slice/livestack-hostd.service' or not re.fullmatch('[a-f0-9]{32}', fields['InvocationID']):
        raise PermissionError('broker_entry_actual_unit_incarnation_required')
    raw = Path('/proc/self/stat').read_text()
    ticks = raw[raw.rfind(')') + 2:].split()[19]
    incarnation = ':'.join((Path('/proc/sys/kernel/random/boot_id').read_text().strip(), fields['InvocationID'], str(os.getpid()), ticks))
    os.environ.clear()
    os.environ.update(selected['environment'])
    os.environ['LIVESTACK_QUALIFICATION_CONFIG'] = selected['qualificationConfiguration']
    os.environ['LIVESTACK_QUALIFICATION_RESERVATIONS'] = selected['reservationPath']
    sys.path.insert(0, value['sourceRoot'])
    from livestack_node.hostd import main as serve
    serve(owner_configuration=selected['ownerConfiguration'], owner_source_identity={'sourceSha256': value['sourceSha256']}, owner_incarnation=incarnation)


if __name__ == '__main__':
    main()
