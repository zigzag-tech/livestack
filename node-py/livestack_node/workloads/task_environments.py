"""Linux-only retained task workspaces for installed workload handlers.

The authority grants one fenced writer at a time. This module keeps the
worker-owned source mirror and declared cache components on a separately
bounded ext4 project-quota filesystem; all process state remains under the
ordinary per-attempt workspace and supervisor.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import time

from .archive import MANIFEST, file_digest, relative_path
from .environment_receipts import REUSE_REASONS
from .model import WorkloadError, encode, name

MAX_REPLICA_BYTES = 32 * 1024**3
MAX_OWNER_BYTES = 128 * 1024**3
MAX_HOST_BYTES = 256 * 1024**3
MAX_ENVIRONMENTS = 64
MAX_METADATA_BYTES = 4096
MAX_COMPONENTS = 16
MAX_MANIFEST_FILES = 50000
# Retained caches use their own bound instead of consuming the source-tree budget.
MAX_CACHE_ENTRIES = MAX_MANIFEST_FILES * 2
MAX_PROBE_BYTES = 8192
LOCK_WAIT_SECONDS = 5
HANDLE = re.compile(r'[a-f0-9]{32}')
_REPLICA_REJECTION_REASONS = {
    'host': 'authority_replica_host_mismatch',
    'profile': 'authority_replica_profile_mismatch',
    'compatibility': 'authority_replica_compatibility_mismatch',
    'generation': 'authority_replica_generation_mismatch',
    'parked': 'authority_replica_not_parked',
}


def _authority_replica_rejection_reason(replica_checks):
    if not replica_checks:
        return 'authority_replica_missing'
    mismatch_sets = [tuple(field for field, matches in check.items() if not matches)
                     for check in replica_checks]
    closest_count = min(len(mismatches) for mismatches in mismatch_sets)
    closest = {mismatches for mismatches in mismatch_sets if len(mismatches) == closest_count}
    if len(closest) == 1:
        failed, = closest
        if len(failed) == 1:
            return _REPLICA_REJECTION_REASONS[failed[0]]
    return 'authority_replica_unconfirmed'


def _reuse_rejection_reason(marker, *, profile, purpose, compatibility, owner_scope, replica_checks):
    if marker is None:
        return _authority_replica_rejection_reason(replica_checks)
    if marker['state'] == 'rebuild_required':
        return 'local_state_untrusted'
    if marker['profile'] != profile:
        return 'profile_changed'
    if marker['purpose'] != purpose:
        return 'purpose_changed'
    if marker['compatibility'] != compatibility:
        return 'toolchain_changed'
    if marker['owner_scope'] != owner_scope:
        return 'owner_scope_changed'
    if marker['state'] != 'parked':
        return 'local_state_untrusted'
    return _authority_replica_rejection_reason(replica_checks)


def _relative(value, field):
    if not isinstance(value, str) or '\\' in value or '\x00' in value:
        raise WorkloadError(f'invalid {field}')
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ('', '.', '..') for part in path.parts) or str(path) != value:
        raise WorkloadError(f'{field} must be a canonical relative path')
    return value


def _inside(root, relative):
    path = root.joinpath(*PurePosixPath(relative).parts)
    if path == root or root not in path.parents:
        raise WorkloadError('environment path escaped its root')
    return path


def _tree_bytes(root, *, deadline=None):
    total, count = 0, 0
    if not root.exists():
        return 0
    for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
        count += len(dirs) + len(files)
        if count > MAX_MANIFEST_FILES or deadline is not None and time.monotonic() > deadline:
            raise WorkloadError('environment inventory exceeded its bounded pass', 503)
        for name_ in dirs + files:
            path = Path(base) / name_
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                total += info.st_blocks * 512
            elif stat.S_ISREG(info.st_mode):
                total += info.st_blocks * 512
            elif not stat.S_ISDIR(info.st_mode):
                raise WorkloadError('special file in retained environment', 503)
    return total


def _remove_tree(path):
    """Remove a private environment tree without following generated links."""
    path = Path(path)
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    def raise_walk_error(error):
        raise error
    for base, dirs, _ in os.walk(path, topdown=False, followlinks=False, onerror=raise_walk_error):
        for item in dirs:
            target = Path(base) / item
            try:
                if target.is_symlink():
                    target.unlink()
                else:
                    target.chmod(stat.S_IMODE(target.stat().st_mode) | 0o700)
            except FileNotFoundError:
                pass
    for base, _, files in os.walk(path, topdown=False, followlinks=False, onerror=raise_walk_error):
        for item in files:
            target = Path(base) / item
            try:
                if target.is_symlink() or not target.is_dir():
                    target.unlink()
                else:
                    target.chmod(stat.S_IMODE(target.stat().st_mode) | 0o700)
            except FileNotFoundError:
                pass
        try:
            Path(base).rmdir()
        except FileNotFoundError:
            pass
    if path.exists() or path.is_symlink():
        raise WorkloadError('retained environment deletion is incomplete', 503)


def _atomic_json(path, value, limit=MAX_METADATA_BYTES):
    raw = encode(value, limit).encode()
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_marker(directory):
    try:
        directory_info = directory.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(directory_info.st_mode):
        return None
    path = directory / 'environment.json'
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_BYTES:
        return None
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    required = {'version', 'handle', 'owner_scope', 'profile', 'purpose', 'compatibility', 'generation',
                'state', 'project_id', 'quota_bytes', 'bytes_used', 'source_digest', 'manifest_digest', 'components',
                'created', 'last_used', 'idle_expires', 'generation_expires'}
    if not isinstance(value, dict) or set(value) != required or value.get('version') != 1 or \
            value.get('handle') != directory.name or not HANDLE.fullmatch(directory.name):
        return None
    if (value['purpose'] not in ('development', 'task_e2e') or
            value['state'] not in ('preparing', 'running', 'awaiting_authority', 'parked', 'rebuild_required') or
            type(value['generation']) is not int or value['generation'] < 0 or
            type(value['project_id']) is not int or type(value['quota_bytes']) is not int or
            not 0 < value['quota_bytes'] <= MAX_REPLICA_BYTES or
            type(value['bytes_used']) is not int or not 0 <= value['bytes_used'] <= value['quota_bytes'] or
            not isinstance(value['owner_scope'], str) or not re.fullmatch('[a-f0-9]{64}', value['owner_scope']) or
            not isinstance(value['profile'], str) or not re.fullmatch('[a-z][a-z0-9_.-]{0,63}', value['profile']) or
            not isinstance(value['compatibility'], str) or
            not re.fullmatch('[a-f0-9]{64}', value['compatibility']) or
            not isinstance(value['components'], dict) or len(value['components']) > MAX_COMPONENTS or
            any(not isinstance(key, str) or not re.fullmatch('[a-z][a-z0-9_.-]{0,63}', key) or
                not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity)
                for key, identity in value['components'].items()) or
            any(isinstance(value[key], bool) or not isinstance(value[key], (int, float)) or
                not math.isfinite(value[key]) or value[key] < 0 for key in
                ('created', 'last_used', 'idle_expires', 'generation_expires')) or
            value['idle_expires'] < value['created'] or value['generation_expires'] < value['created'] or
            not isinstance(value['source_digest'], str) or
            value['source_digest'] != '' and not re.fullmatch('[a-f0-9]{64}', value['source_digest']) or
            not isinstance(value['manifest_digest'], str) or
            value['manifest_digest'] != '' and not re.fullmatch('[a-f0-9]{64}', value['manifest_digest'])):
        return None
    return value


def _manifest(source):
    path = Path(source) / MANIFEST
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024**2:
        raise WorkloadError('captured source manifest is missing or oversized', 409)
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
        records = value['files']
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise WorkloadError('captured source manifest is invalid', 409) from error
    if value.get('version') != 1 or not isinstance(records, list) or len(records) > MAX_MANIFEST_FILES:
        raise WorkloadError('captured source manifest is invalid', 409)
    by_path = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {'path', 'size', 'mode', 'sha256'}:
            raise WorkloadError('captured source manifest record is invalid', 409)
        rel = _relative(record['path'], 'captured source path')
        digest = record['sha256']
        if (rel == MANIFEST or rel in by_path or type(record['size']) is not int or record['size'] < 0 or
                record['mode'] not in (0o644, 0o755) or not isinstance(digest, str) or
                not re.fullmatch('[a-f0-9]{64}', digest)):
            raise WorkloadError('captured source manifest record is invalid', 409)
        by_path[rel] = dict(record)
    return by_path, raw


def _sync_source(incoming, destination, components):
    records, raw_manifest = _manifest(incoming)
    source_links = _source_links(incoming)
    resolved_cache_paths = _resolved_cache_paths(components, source_links)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_roots = set(resolved_cache_paths.values())
    for rel in records:
        if any(rel == root or rel.startswith(root + '/') for root in cache_roots):
            raise WorkloadError('captured source overlaps a retained cache component', 409)
    keep_files = set(records) | {MANIFEST}
    keep_dirs = set()
    for rel in keep_files:
        parts = PurePosixPath(rel).parts[:-1]
        for depth in range(1, len(parts) + 1):
            keep_dirs.add('/'.join(parts[:depth]))
    for cache in cache_roots:
        parts = PurePosixPath(cache).parts
        for depth in range(1, len(parts) + 1):
            keep_dirs.add('/'.join(parts[:depth]))
    deadline = time.monotonic() + 5
    count = 0
    stale_directories = []
    if destination.exists():
        for base, dirs, files in os.walk(destination, topdown=True, followlinks=False):
            relative_base = Path(base).relative_to(destination).as_posix()
            relative_base = '' if relative_base == '.' else relative_base
            descend = []
            for item in dirs:
                rel = item if not relative_base else relative_base + '/' + item
                if rel in cache_roots:
                    continue
                count += 1
                if count > MAX_MANIFEST_FILES * 2 or time.monotonic() > deadline:
                    raise WorkloadError('source mirror reconciliation exceeded its bound', 503)
                target = Path(base) / item
                if target.is_symlink():
                    target.unlink(missing_ok=True)
                    continue
                if rel not in keep_dirs:
                    stale_directories.append(target)
                descend.append(item)
            dirs[:] = descend
            for file in files:
                rel = file if not relative_base else relative_base + '/' + file
                if _cache_root_for(rel, cache_roots):
                    continue
                count += 1
                if count > MAX_MANIFEST_FILES * 2 or time.monotonic() > deadline:
                    raise WorkloadError('source mirror reconciliation exceeded its bound', 503)
                target = Path(base) / file
                if rel not in keep_files or target.is_symlink() or not target.is_file():
                    target.unlink(missing_ok=True)
        for target in sorted(stale_directories, key=lambda path: len(path.parts), reverse=True):
            try:
                target.rmdir()
            except OSError:
                if target.exists() and not any(target.iterdir()):
                    target.rmdir()
    for rel, record in records.items():
        src = _inside(Path(incoming), rel)
        target = _inside(destination, rel)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        unchanged = False
        try:
            info = target.lstat()
            unchanged = (stat.S_ISREG(info.st_mode) and info.st_size == record['size'] and
                         stat.S_IMODE(info.st_mode) == record['mode'] and file_digest(target) == record['sha256'])
        except FileNotFoundError:
            pass
        if not unchanged:
            temporary = target.with_name(target.name + '.sync-tmp')
            shutil.copyfile(src, temporary)
            temporary.chmod(record['mode'])
            os.replace(temporary, target)
    mirror_manifest = destination / MANIFEST
    if not mirror_manifest.exists() or mirror_manifest.is_symlink() or mirror_manifest.read_bytes() != raw_manifest:
        temporary = mirror_manifest.with_name(MANIFEST + '.sync-tmp')
        temporary.write_bytes(raw_manifest)
        temporary.chmod(0o600)
        os.replace(temporary, mirror_manifest)
    return records, hashlib.sha256(raw_manifest).hexdigest()


def _safe_internal_link(path, root):
    """Allow cache links that stay inside the retained source tree.

    npm creates workspace and .bin links in node_modules. They are useful
    retained state, but a cache must not gain a path into the worker or another
    environment. Resolving without requiring the target to exist permits a
    workspace link whose captured alias is restored by the handler later.
    """
    try:
        target = os.readlink(path)
        if os.path.isabs(target) or '\x00' in target:
            return False
        resolved = (Path(path).parent / target).resolve(strict=False)
        root = Path(root).resolve(strict=True)
        return resolved == root or root in resolved.parents
    except (OSError, RuntimeError, ValueError):
        return False


def _has_links(path, root):
    if not path.exists():
        return False
    for base, dirs, files in os.walk(path, topdown=True, followlinks=False):
        for item in dirs + files:
            target = Path(base) / item
            if target.is_symlink() and not _safe_internal_link(target, root):
                return True
    return False


def _source_links(root):
    """Read the optional Benchday portable-source aliases, bounded and closed."""
    path = Path(root) / '.benchday-source.json'
    if not path.exists():
        return {}
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024**2:
        raise WorkloadError('captured source manifest is not a bounded regular file', 409)
    try:
        manifest = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise WorkloadError('captured source manifest is invalid', 409) from error
    links = manifest.get('links') if isinstance(manifest, dict) else None
    if not isinstance(links, list) or len(links) > MAX_MANIFEST_FILES:
        raise WorkloadError('captured source links are invalid', 409)
    result = {}
    for item in links:
        if not isinstance(item, dict) or set(item) != {'path', 'target'}:
            raise WorkloadError('captured source link is invalid', 409)
        link_path = _relative(item['path'], 'captured source link')
        target = _relative(item['target'], 'captured source link target')
        if (link_path in result or link_path == '.benchday-source.json' or
                link_path.startswith('.benchday-cache/') or target == link_path):
            raise WorkloadError('captured source link is duplicated or conflicts with metadata', 409)
        result[link_path] = target
    aliases = set(result)
    for alias in aliases:
        if any(str(parent) in aliases for parent in PurePosixPath(alias).parents):
            raise WorkloadError('captured source aliases overlap', 409)
    for target in result.values():
        target_path = PurePosixPath(target)
        if any(str(parent) in aliases for parent in (target_path, *target_path.parents)):
            raise WorkloadError('captured source alias target is another alias', 409)
    return result


def _resolve_source_alias(path, links):
    path = _relative(path, 'captured source path')
    parts = PurePosixPath(path).parts
    for depth in range(1, len(parts) + 1):
        alias = '/'.join(parts[:depth])
        target = links.get(alias)
        if target is not None:
            return _relative('/'.join((*PurePosixPath(target).parts, *parts[depth:])),
                             'resolved captured source path')
    return path


def _resolved_cache_paths(components, links):
    resolved = {}
    paths = []
    for component in components:
        path = component['path']
        if not path.startswith('source/'):
            raise WorkloadError('task environment cache path is outside source', 409)
        path = _resolve_source_alias(path[len('source/'):], links)
        if any(path == previous or path.startswith(previous + '/') or previous.startswith(path + '/')
               for previous in paths):
            raise WorkloadError('task environment cache components overlap after source alias resolution', 409)
        paths.append(path)
        resolved[component['name']] = path
    return resolved


def _cache_root_for(relative, cache_roots):
    return next((root for root in cache_roots
                 if relative == root or relative.startswith(root + '/')), None)


class TaskEnvironmentStore:
    """One worker host's shared, bounded project-quota environment store."""

    def __init__(self, config, *, workspace, handlers, quota_ensure=None, quota_probe=None, quota_usage=None,
                 require_separate_filesystem=True, filesystem_bytes=None, clock=time.time):
        if sys.platform != 'linux':
            raise WorkloadError('task environments require the Linux project-quota backend')
        if not isinstance(config, dict):
            raise WorkloadError('task environment worker config must be an object')
        allowed = {'root', 'host_id', 'quota_helper', 'quota_prefix', 'project_id_min', 'project_id_max',
                   'max_bytes_per_replica', 'max_bytes_per_owner', 'max_total_bytes', 'reserve_bytes',
                   'idle_seconds', 'generation_seconds', 'max_environments', 'profiles'}
        if set(config) - allowed or not {'root', 'host_id', 'quota_helper', 'project_id_min',
                                         'project_id_max', 'profiles'} <= set(config):
            raise WorkloadError('task environment worker config fields are invalid')
        configured_root = Path(config['root']).expanduser()
        try:
            root_info = configured_root.lstat()
        except OSError as error:
            raise WorkloadError('task environment storage root is unavailable', 503) from error
        if not stat.S_ISDIR(root_info.st_mode):
            raise WorkloadError('task environment storage root must be a real directory', 503)
        self.root = configured_root.resolve(strict=True)
        self.workspace = Path(workspace).resolve()
        # Every worker identity on a physical host mounts a task's current
        # source at this same absolute path inside its attempt namespace. Cargo
        # fingerprints include workspace/target paths, so an attempt-specific
        # bind destination defeats retained native build state.
        self.execution_view = self.root.parent / 'task-environment-view'
        try:
            view_info = self.execution_view.lstat()
        except FileNotFoundError:
            parent_info = self.root.parent.stat()
            if parent_info.st_uid != os.geteuid() or not os.access(self.root.parent, os.W_OK | os.X_OK):
                raise WorkloadError('task environment stable execution view is not provisioned', 503)
            self.execution_view.mkdir(mode=0o700, exist_ok=True)
            view_info = self.execution_view.lstat()
        if (not stat.S_ISDIR(view_info.st_mode) or view_info.st_uid != os.geteuid() or
                view_info.st_gid != os.getegid() or stat.S_IMODE(view_info.st_mode) != 0o700 or
                next(self.execution_view.iterdir(), None) is not None):
            raise WorkloadError('task environment stable execution view is unsafe or not empty', 503)
        self.host_id = name(config['host_id'], 'physical host')
        if require_separate_filesystem and (self.root == self.workspace or
                not self.root.is_mount() or self.root.stat().st_dev == self.workspace.stat().st_dev):
            raise WorkloadError('task environment storage must be a separate bounded filesystem', 503)
        if not self.root.is_dir():
            raise WorkloadError('task environment storage root is unavailable', 503)
        if require_separate_filesystem and (
                root_info.st_uid != 0 or root_info.st_gid != os.getegid() or
                stat.S_IMODE(root_info.st_mode) != 0o1770):
            raise WorkloadError('task environment storage root must be root-owned, sticky, and private to the worker group', 503)
        self.helper = config['quota_helper']
        if not isinstance(self.helper, str) or not self.helper.startswith('/'):
            raise WorkloadError('task environment quota helper must be an absolute installed path')
        prefix = config.get('quota_prefix', ['sudo', '-n'])
        if (not isinstance(prefix, list) or len(prefix) > 4 or
                any(not isinstance(item, str) or not item or '\x00' in item for item in prefix)):
            raise WorkloadError('invalid quota helper privilege prefix')
        self.command = [*prefix, self.helper]
        self.project_id_min, self.project_id_max = config['project_id_min'], config['project_id_max']
        if (type(self.project_id_min) is not int or type(self.project_id_max) is not int or
                not 1 <= self.project_id_min <= self.project_id_max <= 0x7fffffff):
            raise WorkloadError('invalid environment project id range')
        self.max_per_replica = config.get('max_bytes_per_replica', MAX_REPLICA_BYTES)
        self.max_per_owner = config.get('max_bytes_per_owner', MAX_OWNER_BYTES)
        self.max_total = config.get('max_total_bytes', MAX_HOST_BYTES)
        self.reserve_bytes = config.get('reserve_bytes', 1024**3)
        self.max_environments = config.get('max_environments', MAX_ENVIRONMENTS)
        for value, high, field in ((self.max_per_replica, MAX_REPLICA_BYTES, 'per-replica'),
                                   (self.max_per_owner, MAX_OWNER_BYTES, 'per-owner'),
                                   (self.max_total, MAX_HOST_BYTES, 'aggregate')):
            if type(value) is not int or not 1 <= value <= high:
                raise WorkloadError(f'invalid environment {field} byte limit')
        if (type(self.reserve_bytes) is not int or self.reserve_bytes < 0 or
                type(self.max_environments) is not int or not 1 <= self.max_environments <= MAX_ENVIRONMENTS):
            raise WorkloadError('invalid task environment storage bounds')
        fs_bytes = (filesystem_bytes if filesystem_bytes is not None else
                    os.statvfs(self.root).f_blocks * os.statvfs(self.root).f_frsize)
        if type(fs_bytes) is not int or fs_bytes <= 0:
            raise WorkloadError('invalid environment filesystem capacity reading', 503)
        self.max_total = min(self.max_total, max(0, fs_bytes - self.reserve_bytes))
        if self.max_total <= 0:
            raise WorkloadError('task environment filesystem has no retained capacity', 503)
        self.idle_seconds = config.get('idle_seconds', 7 * 86400)
        self.generation_seconds = config.get('generation_seconds', 30 * 86400)
        if (type(self.idle_seconds) not in (int, float) or self.idle_seconds <= 0 or
                type(self.generation_seconds) not in (int, float) or self.generation_seconds <= 0):
            raise WorkloadError('task environment retention must be positive')
        self.profiles = self._profiles(config['profiles'], set(handlers))
        self.quota_ensure = quota_ensure
        self.quota_probe = quota_probe
        self.quota_usage = quota_usage
        self.clock = clock
        self._profile_digests = {}
        self._profile_errors = {}
        self._profile_checked_at = {}
        if not self.root.is_mount() and require_separate_filesystem:
            raise WorkloadError('task environment storage is not a mounted filesystem', 503)
        lock_root = self.root / '.locks'
        lock_root.mkdir(exist_ok=True, mode=0o700)
        lock_info = lock_root.lstat()
        if (not stat.S_ISDIR(lock_info.st_mode) or lock_info.st_uid != os.geteuid() or
                lock_info.st_mode & 0o077 or lock_info.st_dev != self.root.stat().st_dev):
            raise WorkloadError('task environment lock directory is not private worker storage', 503)
        storage_lock = self.root / '.storage.lock'
        try:
            storage_info = storage_lock.lstat()
        except FileNotFoundError:
            storage_info = None
        if storage_info is not None and (not stat.S_ISREG(storage_info.st_mode) or
                                         storage_info.st_uid != os.geteuid() or storage_info.st_mode & 0o077):
            raise WorkloadError('task environment storage lock is unsafe', 503)
        if self._run_helper(['probe'], probe=True) is False:
            raise WorkloadError('task environment project quotas are unavailable', 503)
        self._probe_profiles()

    def _profiles(self, profiles, handlers):
        if not isinstance(profiles, dict) or not 1 <= len(profiles) <= 32:
            raise WorkloadError('task environment profiles must contain 1..32 entries')
        result = {}
        for profile, item in profiles.items():
            name(profile, 'environment profile')
            if not isinstance(item, dict) or set(item) != {
                    'handlers', 'purpose', 'cache_contract', 'probe_argv', 'cache_components'}:
                raise WorkloadError('task environment profile fields are invalid')
            installed = item['handlers']
            if (not isinstance(installed, list) or not installed or len(installed) > 32 or
                    any(handler not in handlers for handler in installed) or len(set(installed)) != len(installed) or
                    item['purpose'] not in ('development', 'task_e2e') or
                    not isinstance(item['cache_contract'], str) or not item['cache_contract'] or
                    len(item['cache_contract']) > 128):
                raise WorkloadError('task environment profile installation is invalid')
            argv = item['probe_argv']
            if (not isinstance(argv, list) or not argv or len(argv) > 32 or
                    any(not isinstance(arg, str) or not arg or '\x00' in arg or len(arg) > 512 for arg in argv) or
                    not Path(argv[0]).is_absolute()):
                raise WorkloadError('task environment compatibility probe must be a fixed absolute argv')
            components = item['cache_components']
            if not isinstance(components, list) or len(components) > MAX_COMPONENTS:
                raise WorkloadError('task environment cache component list exceeds its bound')
            names, paths = set(), set()
            clean = []
            for component in components:
                if not isinstance(component, dict) or set(component) != {'name', 'path', 'inputs', 'contract'}:
                    raise WorkloadError('task environment cache component fields are invalid')
                component_name = name(component['name'], 'cache component')
                path = _relative(component['path'], 'cache component path')
                inputs = component['inputs']
                contract = component['contract']
                if (component_name in names or path in paths or not path.startswith('source/') or
                        not isinstance(inputs, list) or len(inputs) > 128 or
                        any(not isinstance(value, str) for value in inputs) or
                        not isinstance(contract, str) or not 1 <= len(contract) <= 128):
                    raise WorkloadError('task environment cache component declaration is invalid')
                input_paths = [_relative(value, 'cache component input') for value in inputs]
                if any(value.startswith(path[len('source/'):].rstrip('/') + '/') or
                       value == path[len('source/'):] for value in input_paths):
                    raise WorkloadError('cache component inputs overlap its retained output')
                names.add(component_name)
                paths.add(path)
                clean.append(dict(name=component_name, path=path, inputs=input_paths, contract=contract))
            for left in paths:
                if any(right.startswith(left + '/') or left.startswith(right + '/') for right in paths if right != left):
                    raise WorkloadError('retained cache component paths may not overlap')
            result[profile] = dict(handlers=tuple(installed), purpose=item['purpose'],
                cache_contract=item['cache_contract'], probe_argv=tuple(argv), cache_components=tuple(clean))
        return result

    def _run_helper(self, args, *, probe=False, handle=None, project_id=None, bytes_limit=None):
        if self.quota_probe is not None and probe:
            try:
                return bool(self.quota_probe(self.root))
            except Exception as error:
                logging.warning('task_environment_quota_probe_failed: %s: %s', type(error).__name__, error)
                return False
        if self.quota_ensure is not None and not probe:
            return self.quota_ensure(handle, project_id, bytes_limit)
        try:
            reply = subprocess.run([*self.command, *args], capture_output=True, text=True,
                                   timeout=5, env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
            if reply.returncode != 0:
                raise WorkloadError('task environment quota helper refused: ' + reply.stderr.strip()[:512], 503)
            if len(reply.stdout.encode()) > 16 * 1024:
                raise WorkloadError('task environment quota helper response exceeded its bound', 503)
            return json.loads(reply.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError, WorkloadError) as error:
            if probe:
                logging.warning('task_environment_quota_probe_failed: %s: %s', type(error).__name__, error)
                return False
            raise WorkloadError(f'task environment quota operation failed: {type(error).__name__}: {error}', 503) from error

    def _measured_usage(self, project_ids):
        if not project_ids or len(project_ids) > MAX_ENVIRONMENTS or len(set(project_ids)) != len(project_ids):
            raise WorkloadError('task environment quota usage request exceeded its bound', 503)
        if self.quota_usage is not None:
            rows = self.quota_usage(project_ids)
        elif self.quota_ensure is not None:
            # Injected quota callbacks are test backends; production always
            # reads the kernel project quota through the privileged helper.
            rows = [{'project_id': project_id, 'used_bytes': 0,
                     'hard_bytes': MAX_REPLICA_BYTES} for project_id in project_ids]
        else:
            rows = self._run_helper(['usage', *map(str, project_ids)])
        if not isinstance(rows, list) or len(rows) != len(project_ids):
            raise WorkloadError('task environment quota usage reply is invalid', 503)
        result = {}
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'project_id', 'used_bytes', 'hard_bytes'} or
                    type(row['project_id']) is not int or row['project_id'] not in project_ids or
                    type(row['used_bytes']) is not int or row['used_bytes'] < 0 or
                    type(row['hard_bytes']) is not int or row['hard_bytes'] <= 0 or
                    row['project_id'] in result):
                raise WorkloadError('task environment quota usage reply is invalid', 503)
            result[row['project_id']] = dict(used_bytes=row['used_bytes'], hard_bytes=row['hard_bytes'])
        if set(result) != set(project_ids):
            raise WorkloadError('task environment quota usage reply omitted a project', 503)
        return result

    def _probe_profiles(self, selected=None, *, force=False):
        now = time.monotonic()
        profiles = self.profiles if selected is None else {profile: self.profiles[profile] for profile in selected}
        for profile, spec in profiles.items():
            if not force and now - self._profile_checked_at.get(profile, float('-inf')) < 60:
                continue
            try:
                result = subprocess.run(spec['probe_argv'], cwd=self.root, capture_output=True, timeout=10,
                    env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'HOME': str(self.root)},
                    check=False)
                output = result.stdout + result.stderr
                if result.returncode != 0 or len(output) > MAX_PROBE_BYTES:
                    raise WorkloadError('compatibility probe failed or exceeded 8 KiB')
                identity = dict(profile=profile, purpose=spec['purpose'], contract=spec['cache_contract'],
                    os=platform.system(), release=platform.release(), machine=platform.machine(),
                    libc=platform.libc_ver(), python=sys.implementation.cache_tag,
                    probe=hashlib.sha256(output).hexdigest())
                self._profile_digests[profile] = hashlib.sha256(encode(identity).encode()).hexdigest()
                self._profile_errors.pop(profile, None)
            except (OSError, subprocess.TimeoutExpired, WorkloadError) as error:
                self._profile_digests.pop(profile, None)
                self._profile_errors[profile] = f'{type(error).__name__}: {error}'
                logging.warning('task_environment_profile_unavailable: profile=%s reason=%s', profile,
                                self._profile_errors[profile][:512])
            finally:
                self._profile_checked_at[profile] = now

    def report(self):
        """Return known compatible profiles and at most 64 local replica rows."""
        self._probe_profiles()
        profiles = dict(self._profile_digests)
        replicas = []
        deadline = time.monotonic() + 5
        try:
            inventory = self._inventory()
            entries = [path for path, _ in inventory]
            if any(not stat.S_ISDIR(item.lstat().st_mode) for item in entries):
                raise WorkloadError('task environment inventory contains a non-directory entry', 503)
            markers = {directory.name: marker for directory, marker in inventory}
            valid_markers = [marker for marker in markers.values() if marker is not None]
            if len(valid_markers) != len(markers):
                raise WorkloadError('retained environment metadata is corrupt; placement refused', 503)
            if len({marker['project_id'] for marker in valid_markers}) != len(valid_markers):
                raise WorkloadError('retained environment project ids are duplicated', 503)
            usage = self._measured_usage([marker['project_id'] for marker in valid_markers]) if valid_markers else {}
            for directory in entries:
                if time.monotonic() > deadline:
                    raise WorkloadError('task environment inventory exceeded its five second bound', 503)
                marker = _read_marker(directory)
                if marker['state'] != 'parked':
                    continue
                compatibility = profiles.get(marker['profile'])
                if compatibility != marker['compatibility']:
                    continue
                measured = usage[marker['project_id']]
                used = measured['used_bytes']
                expected_hard = ((marker['quota_bytes'] + 1023) // 1024) * 1024
                if measured['hard_bytes'] != expected_hard:
                    raise WorkloadError('retained environment kernel quota differs from its marker', 503)
                if used > marker['quota_bytes']:
                    raise WorkloadError('retained environment exceeds its project quota', 503)
                replicas.append(dict(handle=marker['handle'], profile=marker['profile'],
                    compatibility=marker['compatibility'], generation=marker['generation'], state='parked',
                    bytes_used=used, last_used=marker['last_used']))
        except Exception as error:
            logging.warning('task_environment_inventory_unavailable: %s: %s', type(error).__name__, str(error)[:512])
            return {}, None
        return profiles, replicas

    @contextmanager
    def _locked(self, handle):
        if not HANDLE.fullmatch(handle):
            raise WorkloadError('invalid environment assignment handle', 409)
        global_path = self.root / '.storage.lock'
        lock_path = self.root / '.locks' / (handle + '.lock')
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        env_fd = None
        while env_fd is None:
            fd = os.open(global_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            candidate = None
            acquired = False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                candidate = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    env_fd = candidate
                    candidate = None
                    acquired = True
                except BlockingIOError:
                    pass
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                if candidate is not None:
                    os.close(candidate)
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise WorkloadError('environment_local_writer_busy', 503)
            time.sleep(0.025)
        try:
            yield env_fd
        finally:
            fcntl.flock(env_fd, fcntl.LOCK_UN)
            os.close(env_fd)

    @contextmanager
    def _storage_locked(self):
        """Serialize quota admission and eviction across different handles."""
        fd = os.open(self.root / '.storage.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _inventory(self):
        entries = []
        project_ids = set()
        total_host = 0
        owner_usage = {}
        for path in sorted(self.root.iterdir()):
            if not HANDLE.fullmatch(path.name):
                if path.name == 'lost+found':
                    info = path.lstat()
                    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or
                            stat.S_IMODE(info.st_mode) != 0o700 or info.st_dev != self.root.stat().st_dev):
                        raise WorkloadError('invalid filesystem recovery directory in task environment storage root', 503)
                    continue
                if path.name not in ('.locks', '.storage.lock'):
                    raise WorkloadError('unexpected entry in task environment storage root', 503)
                continue
            if path.is_symlink() or not path.is_dir():
                raise WorkloadError('invalid entry in task environment storage root', 503)
            marker = _read_marker(path)
            if marker is not None:
                if (not self.project_id_min <= marker['project_id'] <= self.project_id_max or
                        marker['quota_bytes'] > self.max_per_replica or marker['project_id'] in project_ids):
                    raise WorkloadError('retained environment exceeds configured quota bounds', 503)
                project_ids.add(marker['project_id'])
                total_host += marker['quota_bytes']
                owner_usage[marker['owner_scope']] = owner_usage.get(marker['owner_scope'], 0) + marker['quota_bytes']
            entries.append((path, marker))
            if len(entries) > self.max_environments:
                raise WorkloadError('task environment host count exceeds its bound', 503)
        if total_host > self.max_total or any(value > self.max_per_owner for value in owner_usage.values()):
            raise WorkloadError('retained environments exceed configured aggregate quota bounds', 503)
        return entries

    def _project_id(self, handle, used):
        width = self.project_id_max - self.project_id_min + 1
        first = self.project_id_min + int.from_bytes(hashlib.sha256(handle.encode()).digest()[:4], 'big') % width
        for offset in range(MAX_ENVIRONMENTS + 1):
            candidate = self.project_id_min + (first - self.project_id_min + offset) % width
            if candidate not in used:
                return candidate
        raise WorkloadError('environment project id capacity exhausted', 503)

    def _quota_for(self, owner_scope, entries, *, current=None):
        total_host, total_owner = 0, 0
        for path, marker in entries:
            if marker is None:
                # A corrupt local marker means its quota reservation is unknown.
                raise WorkloadError('retained environment metadata is corrupt; quota admission refused', 503)
            if current is not None and path.name == current:
                continue
            total_host += marker['quota_bytes']
            if marker['owner_scope'] == owner_scope:
                total_owner += marker['quota_bytes']
        return min(self.max_per_replica, self.max_total-total_host, self.max_per_owner-total_owner)

    def prepare(self, assignment, incoming_source, *, handler):
        """Return a locked environment source path and phase/cache identities."""
        env = assignment.get('environment')
        if not isinstance(env, dict):
            raise WorkloadError('environment assignment is missing', 409)
        required = {'handle', 'purpose', 'profile', 'generation', 'compatibility', 'replicas',
                    'owner_scope', 'queue_seconds'}
        if set(env) != required or type(env['generation']) is not int or env['generation'] <= 0:
            raise WorkloadError('environment assignment fields are invalid', 409)
        handle, profile, generation = env['handle'], env['profile'], env['generation']
        if profile not in self.profiles:
            raise WorkloadError('assigned task environment profile is not installed', 409)
        self._probe_profiles([profile], force=True)
        spec = self.profiles.get(profile)
        compatibility = self._profile_digests.get(profile)
        if (spec is None or compatibility is None or handler not in spec['handlers'] or
                spec['purpose'] != env['purpose']):
            raise WorkloadError('assigned task environment profile is not installed', 409)
        if not isinstance(env['owner_scope'], str) or not re.fullmatch('[a-f0-9]{64}', env['owner_scope']):
            raise WorkloadError('environment owner scope is invalid', 409)
        start = time.monotonic()
        lock_context = self._locked(handle)
        lock_context.__enter__()
        try:
            prepared = self._prepare_locked(assignment, incoming_source, env, spec, compatibility, start)
            prepared['_lock_context'] = lock_context
            return prepared
        except BaseException as error:
            marker_path = self.root / handle / 'environment.json'
            try:
                marker_info = marker_path.parent.lstat()
            except OSError:
                marker_info = None
            marker = (_read_marker(marker_path.parent) if marker_info is not None and
                      stat.S_ISDIR(marker_info.st_mode) else None)
            if marker:
                try:
                    _atomic_json(marker_path, dict(marker, state='rebuild_required', compatibility='0' * 64))
                except Exception as marker_error:
                    logging.warning('task_environment_rebuild_marker_failed: handle=%s error=%s: %s', handle,
                                    type(marker_error).__name__, str(marker_error)[:512])
            elif marker_path.parent.exists():
                try:
                    _remove_tree(marker_path.parent)
                except Exception as cleanup_error:
                    logging.error('task_environment_partial_create_cleanup_failed: handle=%s error=%s: %s', handle,
                                  type(cleanup_error).__name__, str(cleanup_error)[:512])
            lock_context.__exit__(type(error), error, error.__traceback__)
            raise

    def _prepare_locked(self, assignment, incoming_source, env, spec, compatibility, start):
        handle, profile, generation = env['handle'], env['profile'], env['generation']
        current_path = self.root / handle
        authority_replicas = env['replicas']
        with self._storage_locked():
            entries = self._inventory()
            marker = next((entry[1] for entry in entries if entry[0] == current_path), None)
            replica_checks = []
            for replica in authority_replicas:
                if isinstance(replica, dict):
                    replica_checks.append(dict(host=replica.get('host') == self.host_id,
                        profile=replica.get('profile') == profile,
                        compatibility=replica.get('compatibility') == compatibility,
                        generation=replica.get('generation') == (marker or {}).get('generation'),
                        parked=replica.get('state') == 'parked'))
            hit = any(all(check.values()) for check in replica_checks)
            reuse_checks = dict(local_marker=marker is not None, authority_replica=hit,
                parked=bool(marker and marker['state'] == 'parked'),
                profile=bool(marker and marker['profile'] == profile),
                purpose=bool(marker and marker['purpose'] == env['purpose']),
                compatibility=bool(marker and marker['compatibility'] == compatibility),
                owner_scope=bool(marker and marker['owner_scope'] == env['owner_scope']),
                generation_advances=bool(marker and marker['generation'] < generation))
            reusable = all(reuse_checks.values())
            had_local = marker is not None
            discarded_components = dict(marker['components']) if marker is not None else {}
            if marker is not None and marker['generation'] >= generation:
                raise WorkloadError('environment generation is not newer than local replica', 409)
            used_project_ids = {value['project_id'] for _, value in entries if value is not None}
            if reusable:
                meta = dict(marker)
                reuse_outcome = 'reused'
                reason_code = 'compatible_environment_reused'
                reuse_diagnostic = reason_code
            else:
                if current_path.exists():
                    _remove_tree(current_path)
                current_path.mkdir(mode=0o700)
                project_id = self._project_id(handle, used_project_ids)
                quota_bytes = self._quota_for(env['owner_scope'], entries, current=handle)
                if quota_bytes <= 0:
                    current_path.rmdir()
                    raise WorkloadError('environment host storage budget is exhausted', 429)
                if self.quota_ensure is not None:
                    quota_result = self._run_helper([], handle=handle, project_id=project_id,
                                                     bytes_limit=quota_bytes)
                else:
                    quota_result = self._run_helper(['ensure', handle, str(project_id), str(quota_bytes)],
                        handle=handle, project_id=project_id, bytes_limit=quota_bytes)
                if isinstance(quota_result, dict) and quota_result.get('quota_bytes') not in (None, quota_bytes):
                    raise WorkloadError('kernel environment quota differs from the admitted limit', 503)
                now = self.clock()
                meta = dict(version=1, handle=handle, owner_scope=env['owner_scope'], profile=profile,
                    purpose=env['purpose'], compatibility=compatibility, generation=generation,
                    state='preparing', project_id=project_id, quota_bytes=quota_bytes, bytes_used=0,
                    source_digest='', manifest_digest='', components={}, created=now, last_used=now,
                    idle_expires=now+self.idle_seconds, generation_expires=now+self.generation_seconds)
                remote_replica = any(isinstance(value, dict) and value.get('host') != self.host_id and
                                     value.get('state') == 'parked' for value in authority_replicas)
                reuse_outcome = ('relocated' if remote_replica else
                                 'rebuilt' if had_local or generation > 1 else 'created')
                if remote_replica and not had_local:
                    reuse_diagnostic = 'relocated_reconstructed'
                elif had_local:
                    reuse_diagnostic = _reuse_rejection_reason(marker, profile=profile,
                        purpose=env['purpose'], compatibility=compatibility,
                        owner_scope=env['owner_scope'], replica_checks=replica_checks)
                elif generation > 1:
                    reuse_diagnostic = _authority_replica_rejection_reason(replica_checks)
                else:
                    reuse_diagnostic = 'created'
                reason_code = (reuse_diagnostic if reuse_diagnostic in REUSE_REASONS else
                               'authority_replica_unconfirmed')
            replica_summary = ';'.join(','.join(f'{key}={int(value)}' for key, value in check.items())
                                       for check in replica_checks)
            reuse_checks_summary = ','.join(f'{key}={int(value)}' for key, value in reuse_checks.items())
            if not reusable and (had_local or generation > 1):
                logging.warning('task_environment_reuse_rejected: handle=%s generation=%s diagnostic=%s '
                    'checks=%s replica_checks=%s', handle, generation, reuse_diagnostic,
                    reuse_checks_summary[:512], replica_summary[:1024] or 'none')
            logging.info('task_environment_reuse_decision: handle=%s generation=%s checks=%s '
                         'replica_checks=%s reason=%s reused=%s', handle, generation,
                         reuse_checks_summary,
                         replica_summary or 'none', reason_code, int(reusable))
            meta.update(state='preparing', generation=generation, compatibility=compatibility)
            _atomic_json(current_path / 'environment.json', meta)
        source_dir = current_path / 'source'
        source_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        records, manifest_digest = _sync_source(incoming_source, source_dir, spec['cache_components'])
        source_links = _source_links(source_dir)
        resolved_cache_paths = _resolved_cache_paths(spec['cache_components'], source_links)
        current_components = {}
        receipts = []
        for component in spec['cache_components']:
            inputs = []
            for item in component['inputs']:
                resolved_item = _resolve_source_alias(item, source_links)
                record = records.get(resolved_item)
                inputs.append((item, record['sha256'], record['mode']) if record else (item, None))
            input_identity = hashlib.sha256(encode(inputs).encode()).hexdigest()
            identity = hashlib.sha256(encode(dict(profile_compatibility=compatibility,
                cache_contract=spec['cache_contract'], component_contract=component['contract'],
                input_identity=input_identity)).encode()).hexdigest()
            old = meta.get('components', {}).get(component['name']) if reusable else \
                  discarded_components.get(component['name'])
            cache_path = _inside(current_path, f"source/{resolved_cache_paths[component['name']]}")
            outcome = 'reused' if reusable and old == identity and cache_path.exists() and \
                      not _has_links(cache_path, source_dir) else \
                      'invalidated' if old is not None else 'created'
            if outcome != 'reused':
                _remove_tree(cache_path)
                cache_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            current_components[component['name']] = identity
            # `path` is worker-local implementation metadata used by the
            # installed handler to find its declared caches. It is stripped
            # from the authority receipt below; only the stable component name,
            # identity and outcome cross the API boundary.
            receipts.append(dict(name=component['name'], path=component['path'][len('source/'):],
                                 identity=identity, outcome=outcome))
        if reuse_outcome == 'reused' and any(item['outcome'] == 'invalidated' for item in receipts):
            reason_code = 'cache_inputs_changed'
        elif reuse_outcome == 'reused' and marker['source_digest'] != assignment['spec']['input_digest']:
            reason_code = 'source_updated_incrementally'
        meta.update(state='running', generation=generation, compatibility=compatibility,
            source_digest=assignment['spec']['input_digest'], manifest_digest=manifest_digest,
            components=current_components, last_used=self.clock(),
            idle_expires=self.clock()+self.idle_seconds)
        _atomic_json(current_path / 'environment.json', meta)
        source_integrity = {rel: (record['sha256'], record['mode']) for rel, record in records.items()}
        return dict(handle=handle, generation=generation, profile=profile, compatibility=compatibility,
            purpose=env['purpose'], owner_scope=env['owner_scope'], source=source_dir, path=current_path,
            started=start, source_digest=assignment['spec']['input_digest'],
            manifest_digest=manifest_digest,
            source_integrity=source_integrity, reuse_outcome=reuse_outcome, reason_code=reason_code,
            reuse_diagnostic=reuse_diagnostic,
            cache_components=receipts, metadata=meta,
            cache_roots=list(resolved_cache_paths.values()))

    def verify_source(self, prepared):
        root = Path(prepared['source'])
        for rel, (digest, mode) in prepared['source_integrity'].items():
            path = _inside(root, rel)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != mode or file_digest(path) != digest:
                raise WorkloadError('handler modified captured source; environment cannot be parked', 409)
        _, raw_manifest = _manifest(root)
        if hashlib.sha256(raw_manifest).hexdigest() != prepared['manifest_digest']:
            raise WorkloadError('handler removed the captured source manifest', 409)
        expected_links = _source_links(root)
        expected = set(prepared['source_integrity']) | set(expected_links) | {MANIFEST}
        for rel, link_target in expected_links.items():
            path = _inside(root, rel)
            try:
                actual = os.readlink(path)
            except OSError as error:
                raise WorkloadError('captured source link is missing or replaced', 409) from error
            wanted = os.path.relpath(_inside(root, link_target), path.parent)
            if actual != wanted or not _safe_internal_link(path, root):
                raise WorkloadError('captured source link differs from its manifest', 409)
        count = 0
        cache_counts = {cache: 0 for cache in prepared['cache_roots']}
        for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
            relative_base = Path(base).relative_to(root).as_posix()
            relative_base = '' if relative_base == '.' else relative_base
            for item in dirs + files:
                rel = item if not relative_base else relative_base + '/' + item
                target = Path(base) / item
                cache_root = _cache_root_for(rel, cache_counts)
                if cache_root:
                    cache_counts[cache_root] += 1
                    if cache_counts[cache_root] > MAX_CACHE_ENTRIES:
                        raise WorkloadError('retained cache component exceeded its file bound', 413)
                else:
                    count += 1
                    if count > MAX_MANIFEST_FILES * 2:
                        raise WorkloadError('environment source tree exceeded its file bound', 413)
                in_cache = cache_root is not None
                if target.is_symlink():
                    if rel in expected_links:
                        continue
                    if in_cache and _safe_internal_link(target, root):
                        continue
                    raise WorkloadError('handler created an unsafe symlink in retained source or cache state', 409)
                if not target.is_dir() and rel not in expected and not in_cache:
                    raise WorkloadError(f'handler created an undeclared retained source file: {rel[:240]!r}', 409)

    def profile_digest(self, profile):
        return self._profile_digests.get(profile)

    def receipt(self, prepared, *, state, phase_timings):
        if state not in ('parked', 'rebuild_required'):
            raise WorkloadError('invalid environment receipt state')
        measured = self._measured_usage([prepared['metadata']['project_id']])[prepared['metadata']['project_id']]
        expected_hard = ((prepared['metadata']['quota_bytes'] + 1023) // 1024) * 1024
        if measured['hard_bytes'] != expected_hard:
            raise WorkloadError('retained environment kernel quota differs from its marker', 503)
        used = measured['used_bytes']
        if used > prepared['metadata']['quota_bytes']:
            raise WorkloadError('retained environment exceeds its project quota', 503)
        return dict(version=1, handle=prepared['handle'], generation=prepared['generation'],
            profile=prepared['profile'], compatibility=prepared['compatibility'],
            source_digest=prepared['source_digest'], reuse_outcome=prepared['reuse_outcome'],
            reason_code=prepared['reason_code'], state=state, bytes_used=used, phase_timings=phase_timings,
            cache_components=[{key: component[key] for key in ('name', 'identity', 'outcome')}
                              for component in prepared['cache_components']])

    def mark_awaiting_authority(self, prepared, *, state, bytes_used):
        if state not in ('parked', 'rebuild_required') or not 0 <= bytes_used <= prepared['metadata']['quota_bytes']:
            raise WorkloadError('invalid retained environment handoff', 409)
        meta = dict(prepared['metadata'], state='awaiting_authority' if state == 'parked' else 'rebuild_required',
                    compatibility=prepared['compatibility'] if state == 'parked' else '0' * 64,
                    bytes_used=bytes_used, last_used=self.clock())
        _atomic_json(Path(prepared['path']) / 'environment.json', meta)
        prepared['metadata'] = meta

    def acknowledge(self, prepared, authority_environment):
        if (not isinstance(authority_environment, dict) or authority_environment.get('state') != 'parked' or
                authority_environment.get('handle') != prepared['handle'] or
                authority_environment.get('generation') != prepared['generation']):
            return False
        path = Path(prepared['path']) / 'environment.json'
        marker = _read_marker(path.parent)
        if (marker is None or marker['generation'] != prepared['generation'] or
                marker['state'] != 'awaiting_authority'):
            return False
        meta = dict(prepared['metadata'], state='parked', last_used=self.clock())
        _atomic_json(path, meta)
        return True

    def acknowledge_handle(self, handle, generation, authority_environment):
        if (not isinstance(authority_environment, dict) or authority_environment.get('state') != 'parked' or
                authority_environment.get('handle') != handle or
                authority_environment.get('generation') != generation):
            return False
        with self._locked(handle):
            directory = self.root / handle
            marker = _read_marker(directory) if directory.exists() else None
            if (marker is None or marker['generation'] != generation or
                    marker['state'] != 'awaiting_authority'):
                return False
            _atomic_json(directory / 'environment.json', dict(marker, state='parked', last_used=self.clock()))
            return True

    def reject(self, prepared):
        try:
            meta = dict(prepared['metadata'], state='rebuild_required', compatibility='0' * 64)
            _atomic_json(Path(prepared['path']) / 'environment.json', meta)
        except Exception as error:
            logging.warning('task_environment_fence_marker_failed: handle=%s error=%s: %s',
                            prepared.get('handle'), type(error).__name__, str(error)[:512])

    def invalidate(self, handle):
        if not HANDLE.fullmatch(handle):
            raise WorkloadError('invalid environment handle for recovery', 409)
        with self._locked(handle):
            directory = self.root / handle
            marker = _read_marker(directory) if directory.exists() else None
            if marker:
                _atomic_json(directory / 'environment.json',
                             dict(marker, state='rebuild_required', compatibility='0' * 64))

    def remove_stale_replica(self, handle, generation):
        """Remove only the exact local generation rejected by authority.

        A nonblocking per-handle lock keeps a returning worker from deleting a
        replica another worker on this physical host has already started using.
        The marker check is repeated under both storage locks, so an obsolete
        report cannot remove a newer local generation.
        """
        if not HANDLE.fullmatch(handle) or type(generation) is not int or generation < 0:
            raise WorkloadError('invalid stale environment cleanup identity', 409)
        global_path = self.root / '.storage.lock'
        lock_path = self.root / '.locks' / (handle + '.lock')
        global_fd = os.open(global_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(global_fd, fcntl.LOCK_EX)
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return 'busy'
                directory = self.root / handle
                try:
                    directory_info = directory.lstat()
                except FileNotFoundError:
                    lock_path.unlink(missing_ok=True)
                    return 'already_absent'
                if not stat.S_ISDIR(directory_info.st_mode):
                    raise WorkloadError('stale task environment path is not a real directory', 503)
                marker = _read_marker(directory)
                if marker is None or marker['handle'] != handle or marker['generation'] != generation:
                    return 'changed'
                _remove_tree(directory)
                lock_path.unlink(missing_ok=True)
                return 'removed'
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        finally:
            fcntl.flock(global_fd, fcntl.LOCK_UN)
            os.close(global_fd)

    def release(self, prepared):
        # The caller invokes this only after the supervisor has proved the
        # attempt's entire cgroup/container tree is stopped.
        context = prepared.pop('_lock_context', None)
        if context is not None:
            context.__exit__(None, None, None)

    def prune(self, *, rows=64, seconds=5):
        if not 1 <= rows <= 64 or not 0 < seconds <= 5:
            raise ValueError('environment sweep bounds may only be lowered')
        started = time.monotonic()
        removed, checked = [], 0
        global_path = self.root / '.storage.lock'
        fd = os.open(global_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            for directory, meta in self._inventory():
                if checked >= rows or time.monotonic() - started >= seconds:
                    break
                checked += 1
                if meta is None:
                    continue
                now = self.clock()
                expired = now >= meta['idle_expires'] or now >= meta['generation_expires']
                if meta['state'] != 'rebuild_required' and not expired:
                    continue
                lock_path = self.root / '.locks' / (directory.name + '.lock')
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    _remove_tree(directory)
                    lock_path.unlink(missing_ok=True)
                    removed.append(directory.name)
                except Exception as error:
                    logging.warning('task_environment_eviction_failed: handle=%s reason=%s: %s',
                                    directory.name, type(error).__name__, str(error)[:512])
                finally:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        logging.info('task_environment_sweep: examined=%d removed=%d elapsed_ms=%.1f',
                     checked, len(removed), (time.monotonic() - started) * 1000)
        return dict(examined=checked, removed=removed, elapsed_seconds=time.monotonic() - started)
