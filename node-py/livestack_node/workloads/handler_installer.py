"""Verified, atomic worker-side installation for immutable handler packages."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import tarfile
import tempfile
import time

from .handler_release import MAX_PACKAGE_BYTES, validate_manifest
from .model import WorkloadError, encode

MAX_INSTALLED_BYTES = 16 * 1024**3
MAX_INSTALLED_PACKAGES = 256
MAX_TRANSIENT_ENTRIES = 3
MAX_PACKAGE_ROOT_ENTRIES = MAX_INSTALLED_PACKAGES + MAX_TRANSIENT_ENTRIES + 1


def _digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_size(root):
    total = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        if any((Path(base) / name).is_symlink() for name in dirs):
            raise WorkloadError('handler_install_symlink_refused', 409)
        for name in files:
            path = Path(base) / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise WorkloadError('handler_install_special_file_refused', 409)
            total += info.st_size
    return total


class HandlerPackageStore:
    def __init__(self, root, runtimes, base_handlers, *, platform_name=None, architecture=None):
        requested_root = Path(root).expanduser()
        if requested_root.is_symlink():
            raise WorkloadError('handler_registry_directory_symlink_refused', 503)
        self.root = requested_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root_info = self.root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) & 0o077:
            raise WorkloadError('handler_registry_directory_permissions_invalid', 503)
        if not isinstance(runtimes, dict) or len(runtimes) > 16:
            raise WorkloadError('handler_runtime_configuration_invalid')
        self._recover_transient_entries()
        self.runtimes = {}
        for runtime_id, executable in runtimes.items():
            path = Path(executable).resolve()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise WorkloadError('handler_runtime_not_installed: '+str(runtime_id))
            self.runtimes[runtime_id] = str(path)
        self.base_handlers = base_handlers
        self._verified_manifests = {}
        self.platform = platform_name or ({'Linux': 'linux', 'Darwin': 'darwin', 'Windows': 'windows'}
                                          .get(platform.system(), 'unsupported'))
        machine = (architecture or platform.machine()).lower()
        self.architecture = {'x86_64': 'x86_64', 'amd64': 'x86_64', 'aarch64': 'aarch64',
                             'arm64': 'arm64'}.get(machine, machine)
        self.pointer = self.root/'registry-current.json'

    def _recover_transient_entries(self):
        entries = list(self.root.iterdir())
        if len(entries) > MAX_PACKAGE_ROOT_ENTRIES:
            raise WorkloadError('handler_registry_directory_capacity', 503)
        transient = [path for path in entries if path.name == '.registry-current.tmp' or
                     path.name.startswith('.handler-stage-') or path.name.startswith('.handler-download-')]
        if len(transient) > MAX_TRANSIENT_ENTRIES:
            raise WorkloadError('handler_install_staging_capacity', 503)
        for path in transient:
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                if _tree_size(path) > MAX_PACKAGE_BYTES:
                    raise WorkloadError('handler_install_staging_bytes_exceeded', 503)
                self._make_tree_writable(path)
                shutil.rmtree(path)
            elif stat.S_ISREG(info.st_mode) and info.st_size <= MAX_PACKAGE_BYTES:
                path.unlink()
            else:
                raise WorkloadError('handler_install_staging_entry_invalid', 503)

    def _package(self, digest):
        if len(digest) != 64 or any(ch not in '0123456789abcdef' for ch in digest):
            raise WorkloadError('handler_release_digest_invalid')
        return self.root/digest

    def _manifest(self, package):
        try:
            value = json.loads((package/'.manifest.json').read_bytes())
            checked = validate_manifest(value['manifest'], value['release_digest'])
            if checked['release_digest'] != package.name:
                raise WorkloadError('handler_install_identity_mismatch', 409)
            return checked['manifest']
        except WorkloadError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise WorkloadError('handler_installed_manifest_invalid', 409) from error

    def verify_installed(self, digest):
        package = self._package(digest)
        manifest = self._manifest(package)
        payload = package/'payload'
        expected = {item['path']: item for item in manifest['files']}
        seen, total = set(), 0
        for item in manifest['files']:
            rel = PurePosixPath(item['path'])
            path = payload.joinpath(*rel.parts)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size != item['size']:
                raise WorkloadError('handler_installed_file_invalid', 409)
            if os.name != 'nt' and stat.S_IMODE(info.st_mode) != item['mode']:
                raise WorkloadError('handler_installed_mode_mismatch', 409)
            if _digest_file(path) != item['sha256']:
                raise WorkloadError('handler_installed_file_digest_mismatch', 409)
            seen.add(item['path'])
            total += info.st_size
        actual = set()
        for base, _, files in os.walk(payload, followlinks=False):
            for name in files:
                path = Path(base)/name
                if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                    raise WorkloadError('handler_installed_special_file_refused', 409)
                actual.add(path.relative_to(payload).as_posix())
        if actual != set(expected) or seen != set(expected) or total > MAX_PACKAGE_BYTES:
            raise WorkloadError('handler_installed_inventory_mismatch', 409)
        self._verified_manifests[digest] = manifest
        return manifest

    def manifest(self, digest, *, verify=False):
        if verify or digest not in self._verified_manifests:
            return self.verify_installed(digest)
        return self._verified_manifests[digest]

    def install(self, archive_path, descriptor):
        manifest = validate_manifest(descriptor['manifest'], descriptor['release_digest'])['manifest']
        digest = descriptor['release_digest']
        archive_path = Path(archive_path)
        if archive_path.stat().st_size != descriptor['archive_bytes'] or _digest_file(archive_path) != descriptor['archive_digest']:
            raise WorkloadError('handler_archive_digest_mismatch', 409)
        canonical_size = 1024 + sum(512 + ((item['size'] + 511)//512)*512 for item in manifest['files'])
        archive_size = archive_path.stat().st_size
        if archive_size < canonical_size or archive_size % 512:
            raise WorkloadError('handler_archive_noncanonical_size', 400)
        with archive_path.open('rb') as stream:
            stream.seek(archive_size-1024)
            if stream.read(1024) != bytes(1024):
                raise WorkloadError('handler_archive_noncanonical_trailer', 400)
        final = self._package(digest)
        if final.exists():
            existing = self.verify_installed(digest)
            if existing != manifest:
                raise WorkloadError('handler_release_digest_collision', 409)
            self._verified_manifests[digest] = existing
            return final
        installed = sum(_tree_size(path) for path in self.root.iterdir()
                        if path.is_dir() and len(path.name) == 64 and all(ch in '0123456789abcdef' for ch in path.name))
        count = sum(1 for path in self.root.iterdir()
                    if path.is_dir() and len(path.name) == 64 and all(ch in '0123456789abcdef' for ch in path.name))
        if count >= MAX_INSTALLED_PACKAGES or installed + descriptor['archive_bytes'] > MAX_INSTALLED_BYTES:
            raise WorkloadError('handler_worker_package_capacity', 429)
        staging = Path(tempfile.mkdtemp(prefix='.handler-stage-', dir=self.root))
        try:
            payload = staging/'payload'
            payload.mkdir(mode=0o700)
            expected = {item['path']: item for item in manifest['files']}
            seen, expanded = set(), 0
            with tarfile.open(archive_path, mode='r:') as archive:
                for member in archive:
                    name = member.name
                    rel = PurePosixPath(name)
                    if (not member.isfile() or name in seen or rel.is_absolute() or '\\' in name or
                            str(rel) != name or any(part in ('', '.', '..') for part in rel.parts)):
                        raise WorkloadError('handler_archive_member_refused', 400)
                    item = expected.get(name)
                    if item is None or member.size != item['size'] or (member.mode & 0o7777) != item['mode']:
                        raise WorkloadError('handler_archive_inventory_mismatch', 409)
                    destination = payload.joinpath(*rel.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    source = archive.extractfile(member)
                    if source is None:
                        raise WorkloadError('handler_archive_member_unreadable', 400)
                    hasher, size = hashlib.sha256(), 0
                    with source, destination.open('xb') as out:
                        for chunk in iter(lambda: source.read(1024*1024), b''):
                            size += len(chunk)
                            expanded += len(chunk)
                            if expanded > MAX_PACKAGE_BYTES:
                                raise WorkloadError('handler_package_bytes_exceeded', 413)
                            hasher.update(chunk)
                            out.write(chunk)
                    if size != item['size'] or hasher.hexdigest() != item['sha256']:
                        raise WorkloadError('handler_archive_file_digest_mismatch', 409)
                    destination.chmod(item['mode'])
                    seen.add(name)
            if seen != set(expected):
                raise WorkloadError('handler_archive_inventory_mismatch', 409)
            (staging/'.manifest.json').write_text(encode(dict(manifest=manifest, release_digest=digest), 4*1024**2))
            self._make_tree_readonly(staging)
            os.replace(staging, final)
            self._verified_manifests[digest] = manifest
            return final
        finally:
            if staging.exists():
                self._make_tree_writable(staging)
                shutil.rmtree(staging, ignore_errors=True)

    def _make_tree_readonly(self, root):
        for base, dirs, files in os.walk(root, topdown=False, followlinks=False):
            for name in files:
                os.chmod(Path(base)/name, 0o444 if not os.stat(Path(base)/name).st_mode & 0o111 else 0o555)
            for name in dirs:
                os.chmod(Path(base)/name, 0o555)
        os.chmod(root, 0o555)

    def _make_tree_writable(self, root):
        for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
            os.chmod(base, 0o700)
            for name in dirs:
                path = Path(base)/name
                if not path.is_symlink():
                    os.chmod(path, 0o700)
            for name in files:
                path = Path(base)/name
                if not path.is_symlink():
                    os.chmod(path, 0o600)

    def read_pointer(self, *, verify=True):
        if not self.pointer.exists():
            return {'generation': 0, 'defaults': {}}
        try:
            state = json.loads(self.pointer.read_bytes())
            if (not isinstance(state, dict) or set(state) != {'generation', 'defaults'} or
                    type(state['generation']) is not int or state['generation'] < 0 or
                    not isinstance(state['defaults'], dict) or len(state['defaults']) > 64):
                raise ValueError('invalid pointer')
            for handler, digest in state['defaults'].items():
                if self.manifest(digest, verify=verify)['handler_id'] != handler:
                    raise ValueError('pointer identity mismatch')
            return state
        except (OSError, ValueError, TypeError) as error:
            raise WorkloadError('handler_worker_registry_pointer_invalid', 503) from error

    def commit_pointer(self, generation, defaults):
        state = {'generation': generation, 'defaults': dict(sorted(defaults.items()))}
        for handler, digest in state['defaults'].items():
            manifest = self.verify_installed(digest)
            if manifest['handler_id'] != handler:
                raise WorkloadError('handler_worker_registry_identity_mismatch', 409)
        temporary = self.root/'.registry-current.tmp'
        with temporary.open('wb') as stream:
            stream.write(encode(state, 64*1024).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.pointer)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def inventory(self, generation, defaults=None, *, verify=False):
        releases = []
        for package in self.root.iterdir():
            if not package.is_dir() or len(package.name) != 64 or any(ch not in '0123456789abcdef' for ch in package.name):
                continue
            manifest = self.manifest(package.name, verify=verify)
            releases.append(dict(handler_id=manifest['handler_id'], release_digest=package.name,
                execution_contract=manifest['execution_contract'], payload_schema=manifest['payload_schema'],
                result_schema=manifest['result_schema']))
            if len(releases) > MAX_INSTALLED_PACKAGES:
                raise WorkloadError('handler_worker_inventory_capacity', 503)
        releases.sort(key=lambda item: (item['handler_id'], item['release_digest']))
        if defaults is None:
            defaults = self.read_pointer(verify=False)['defaults']
        return {'generation': generation, 'defaults': dict(sorted(defaults.items())), 'releases': releases}

    def prune(self, required_digests, protected_digests, retention_seconds, *, generation,
              references_complete):
        if references_complete is not True:
            return dict(outcome='refused', reason='handler_release_reference_evidence_unavailable',
                        generation=generation, examined=0, deleted=0, bytes_reclaimed=0,
                        deleted_digests=[], reference_evidence='unavailable')
        if (type(retention_seconds) is not int or retention_seconds < 24*60*60):
            return dict(outcome='refused', reason='handler_release_retention_unconfigured',
                        generation=generation, examined=0, deleted=0, bytes_reclaimed=0,
                        deleted_digests=[], reference_evidence='complete')
        keep = set(required_digests) | set(protected_digests)
        now = time.time()
        examined = deleted = reclaimed = 0
        removed = []
        for package in self.root.iterdir():
            if not package.is_dir() or len(package.name) != 64 or any(ch not in '0123456789abcdef' for ch in package.name):
                continue
            examined += 1
            if package.name in keep or now-package.stat().st_mtime < retention_seconds:
                continue
            size = _tree_size(package)
            self._make_tree_writable(package)
            shutil.rmtree(package)
            self._verified_manifests.pop(package.name, None)
            deleted += 1
            reclaimed += size
            removed.append(package.name)
        return dict(outcome='complete', reason=None, generation=generation, examined=examined,
                    deleted=deleted, bytes_reclaimed=reclaimed, deleted_digests=removed[:MAX_INSTALLED_PACKAGES],
                    reference_evidence='complete')

    def handler_config(self, digest, *, verify=True):
        manifest = self.manifest(digest, verify=verify)
        runtime = self.runtimes.get(manifest['runtime_id'])
        if runtime is None:
            raise WorkloadError('handler_runtime_not_installed: '+manifest['runtime_id'], 409)
        if manifest['platform'] != self.platform or manifest['architecture'] != self.architecture:
            raise WorkloadError('handler_runtime_platform_mismatch', 409)
        base = self.base_handlers.get(manifest['handler_id'])
        if base is None:
            raise WorkloadError('handler_id_not_authorized_by_worker', 403)
        if manifest['backend'] != base.get('backend', 'native'):
            raise WorkloadError('handler_backend_policy_mismatch', 403)
        package = self._package(digest)
        argv = [runtime, str(package/'payload'/manifest['entrypoint']), *manifest['arguments']]
        result = dict(base)
        result.update(argv=argv, outputs=manifest['outputs'],
                      infrastructure_outputs=manifest['infrastructure_outputs'],
                      infrastructure_exit_codes=manifest['infrastructure_exit_codes'],
                      backend=manifest['backend'], release_digest=digest)
        return result
