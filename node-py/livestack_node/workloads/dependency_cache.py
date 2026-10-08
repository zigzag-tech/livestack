"""Persistent, bounded, per-principal cache of installed dependency trees.

Design: openspec/changes/dependency-cache, operator doc: node-py/docs/dependency-cache.md.

A job's source declares which directories it installs (for example `node_modules`)
and which files determine their content (its lockfile). Before the handler starts
the worker copies a matching entry into the attempt's source tree; when the handler
finishes it names the trees it installed completely and the worker stores the ones
that missed. Everything runs on the worker, in the host user namespace, outside the
handler's sandbox; the handler only sees ordinary directories and one environment
variable naming what was restored.

Nothing here may fail an attempt: `restore` and `save` return named outcomes and
swallow their own faults, and an unusable cache is exactly today's cold install.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import time

from .docker_cache import namespace

SCHEMA = 1
MANIFEST = '.livestack/dependency-cache.json'
COMMIT_FILE = 'dependency-cache-commit.json'
ENV_HANDSHAKE = 'HARMONY_DEPENDENCY_CACHE_HANDSHAKE'
REQUEST_FILE = 'request.json'
RESPONSE_FILE = 'response.json'
KEYS = {'enabled', 'path', 'max_bytes', 'epoch', 'refresh_every', 'max_component_bytes'}
MAX_FILE = 16384
MAX_COMPONENTS = 16
MAX_ENTRIES = 64                 # expanded (per-directory) entries per attempt
MAX_KEY_FILES = 20000            # files hashed for one directory key path
MAX_TREE_FILES = 400000
RECORDS = 32
TEMP_AGE = 3600


# ------------------------------------------------------------------ settings

def settings(raw):
    """Validate worker.json `dependency_cache`; returns (settings or None, reason)."""
    if raw is None:
        return None, 'not-configured'
    if not isinstance(raw, dict):
        return None, 'invalid: not an object'
    unknown = set(raw) - KEYS
    if unknown:
        return None, 'invalid: unknown keys %s' % sorted(unknown)
    if raw.get('enabled') is not True:
        if 'enabled' in raw and not isinstance(raw['enabled'], bool):
            return None, 'invalid: enabled must be a boolean'
        return None, 'disabled'
    path = raw.get('path')
    if (not isinstance(path, str) or not path.startswith('/') or '\n' in path or ':' in path or
            '\x00' in path or '..' in Path(path).parts or len(path) > 200):
        return None, 'invalid: path must be a short absolute path'

    def number(key, low, high, default):
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError('%s must be an integer from %s to %s' % (key, low, high))
        return value
    try:
        out = dict(path=str(Path(path)), max_bytes=number('max_bytes', 64*1024**2, 4*1024**4, 8*1024**3),
                   epoch=number('epoch', 0, 2**31, 0), refresh_every=number('refresh_every', 0, 1000, 20),
                   max_component_bytes=number('max_component_bytes', 1024**2, 64*1024**3, 4*1024**3))
    except ValueError as error:
        return None, 'invalid: %s' % error
    return out, 'enabled'


def opted_in(handler):
    """(True, None) when the worker's handler entry says `"dependency_cache": true`, else (False, reason).

    Opt-in lives in the worker-owned handler entry (worker.json `handlers`), never in the job's source or
    the handler package: whoever operates the worker decides which handlers pay for restores. Anything
    other than the boolean true (absent, false, a string, 1) is NOT opted in.
    """
    if 'dependency_cache' not in handler:
        return False, 'not-opted-in'
    if handler['dependency_cache'] is True:
        return True, None
    if handler['dependency_cache'] is False:
        return False, 'not-opted-in'
    return False, 'invalid: dependency_cache must be a boolean'


# ------------------------------------------------------------------ manifest

def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value or len(value) > 256:
        return False
    path = PurePosixPath(value.replace('{}', 'x'))
    return (not path.is_absolute() and all(part not in ('', '.', '..') for part in path.parts)
            and str(path) == value.replace('{}', 'x'))


def load_manifest(source):
    """The declared components, or ([], reason). `source` is the attempt's input directory.

    {"version": 1, "components": [{"path": "{}/node_modules", "for_each": ["hub", "packages/*"],
                                   "key_paths": ["{}/package.json", "{}/package-lock.json"]}]}
    `{}` stands for each directory `for_each` matches (`*` only in its last segment);
    a component without `for_each` names one literal path and uses no `{}`.
    """
    path = Path(source)/MANIFEST
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [], 'no-manifest'
    except OSError as error:
        return [], 'unreadable-manifest: %s' % error.strerror
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
        return [], 'invalid-manifest: not a small regular file'
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        return [], 'invalid-manifest: %s' % error
    if (not isinstance(raw, dict) or set(raw) != {'version', 'components'} or raw['version'] != SCHEMA or
            not isinstance(raw['components'], list) or not 0 < len(raw['components']) <= MAX_COMPONENTS):
        return [], 'invalid-manifest: shape'
    components = []
    for item in raw['components']:
        if (not isinstance(item, dict) or not set(item) <= {'path', 'for_each', 'key_paths'} or
                not {'path', 'key_paths'} <= set(item)):
            return [], 'invalid-manifest: component shape'
        each = item.get('for_each')
        keys = item['key_paths']
        if (not _relative(item['path']) or not isinstance(keys, list) or not 0 < len(keys) <= 8 or
                not all(_relative(key) for key in keys) or
                (each is not None and (not isinstance(each, list) or not 0 < len(each) <= 32 or
                                       not all(_relative(root) and '{}' not in root for root in each)))):
            return [], 'invalid-manifest: component values'
        templated = '{}' in item['path']
        if templated != (each is not None) or any(('{}' in key) != templated for key in keys):
            return [], 'invalid-manifest: {} must appear in path and key_paths exactly when for_each is given'
        components.append(dict(path=item['path'], key_paths=keys, for_each=each))
    return components, 'ok'


def _expand_root(source, pattern):
    """Directories under `source` matching a pattern whose only wildcard is its last segment."""
    parts = pattern.split('/')
    if any('*' in part for part in parts[:-1]):
        return None
    if '*' not in parts[-1]:
        return [pattern] if (Path(source)/pattern).is_dir() else []
    parent = Path(source).joinpath(*parts[:-1])
    try:
        names = sorted(name for name in os.listdir(parent) if re.fullmatch(re.escape(parts[-1]).replace(r'\*', '[^/]*'), name))
    except OSError:
        return []
    return ['/'.join(parts[:-1] + [name]) for name in names if (parent/name).is_dir() and not (parent/name).is_symlink()]


def expand(source, components):
    """[(path, key_paths)] for every concrete tree, or (None, reason) when a pattern is unsupported."""
    out = []
    for item in components:
        if item['for_each'] is None:
            out.append((item['path'], item['key_paths']))
            continue
        for pattern in item['for_each']:
            roots = _expand_root(source, pattern)
            if roots is None:
                return None, 'unsupported-pattern: %s' % pattern
            for root in roots:
                out.append((item['path'].replace('{}', root), [key.replace('{}', root) for key in item['key_paths']]))
    if len(out) > MAX_ENTRIES:
        return None, 'too-many-entries'
    return out, 'ok'


# ---------------------------------------------------------------------- keys

def _digest_path(source, relative):
    """sha256 of a regular file, or of a directory's sorted (path, file digest) list; None when absent/unsafe."""
    base = Path(source)/relative
    try:
        info = base.lstat()
    except OSError:
        return None
    if stat.S_ISREG(info.st_mode):
        return hashlib.sha256(base.read_bytes()).hexdigest()
    if not stat.S_ISDIR(info.st_mode):
        return None
    digest, count = hashlib.sha256(), 0
    for current, dirs, files in os.walk(base, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            count += 1
            path = Path(current)/name
            if count > MAX_KEY_FILES or not stat.S_ISREG(path.lstat().st_mode):
                return None
            digest.update(('%s\0%s\0' % (path.relative_to(base).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())).encode())
    return digest.hexdigest()


def toolchain(argv0):
    """Identity of the executable that will run the handler plus the host ABI; part of every key.

    Installed trees can hold compiled addons, so a different node, libc or CPU must not match.
    Stat, not exec: nothing a job declares is ever run by the worker.
    """
    try:
        real = os.path.realpath(argv0)
        info = os.stat(real)
        exe = '%s:%d:%d' % (real, info.st_size, info.st_mtime_ns)
    except OSError:
        exe = 'unknown'
    return '%s|%s|%s|%s' % (exe, platform.system(), platform.machine(), '-'.join(platform.libc_ver()))


def entry_key(config, owner, tool, path, source, key_paths):
    """Hex key for one tree, or None when any declared key path is missing or unsafe (never cached)."""
    parts = []
    for relative in key_paths:
        value = _digest_path(source, relative)
        if value is None:
            return None
        parts.append((relative, value))
    material = json.dumps([SCHEMA, config['epoch'], namespace(owner), tool, path, parts], sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()


# --------------------------------------------------------------------- trees

def scan(root, source_root=None):
    """(files, bytes, error) of a tree; never follows links. error names the first unsafe thing.

    A symlink must be relative and, when `source_root` is given, stay inside it: an absolute
    link would point into the attempt that saved it.
    """
    files, total, base = 0, 0, Path(root)
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as error:
            return files, total, 'unreadable: %s' % (error.strerror or error)
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                return files, total, 'unreadable: %s' % (error.strerror or error)
            files += 1
            if files > MAX_TREE_FILES:
                return files, total, 'too-many-files'
            mode = info.st_mode
            if stat.S_ISDIR(mode):
                stack.append(Path(entry.path))
            elif stat.S_ISLNK(mode):
                target = os.readlink(entry.path)
                if os.path.isabs(target):
                    return files, total, 'absolute-symlink'
                if source_root is not None:
                    resolved = os.path.normpath(os.path.join(os.path.dirname(entry.path), target))
                    if os.path.commonpath([resolved, str(source_root)]) != str(source_root):
                        return files, total, 'symlink-escapes-source'
            elif stat.S_ISREG(mode):
                total += info.st_blocks * 512
            else:
                return files, total, 'special-file'
    return files, total, None


def _copy(source, destination):
    """Copy a tree (links kept as links, modes and times kept); reflink where the filesystem has it."""
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['cp', '-a', '--reflink=auto', '--', str(source), str(destination)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)


def _remove(path):
    def fix(function, target, _):
        os.chmod(os.path.dirname(target), 0o700)
        os.chmod(target, 0o700)
        function(target)
    path = Path(path)
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path, onerror=fix)


def _write_json(path, value):
    temporary = Path('%s.%d.tmp' % (path, os.getpid()))
    temporary.write_text(json.dumps(value, sort_keys=True))
    os.replace(temporary, path)


def _read_json(path):
    try:
        if Path(path).is_symlink() or Path(path).stat().st_size > MAX_FILE:
            return None
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------- cache

class Attempt:
    """One attempt's view of the cache: restore before the handler, save after it."""

    def __init__(self, config, owner, attempt, handler_argv0):
        self.config, self.owner, self.attempt = config, owner, str(attempt)
        self.tool = toolchain(handler_argv0)
        self.root = Path(config['path'])/namespace(owner)
        self.records = []
        self.entries = []        # [(path, key_paths, key, outcome)] after restore
        self.served = False
        self.source = None       # the handler's root, once it has asked

    # -- restore
    def _refresh_due(self):
        every = self.config['refresh_every']
        return bool(every) and int(hashlib.sha256(self.attempt.encode()).hexdigest(), 16) % every == 0

    def _lock(self, exclusive):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = open(self.root/'.lock', 'a+')
        try:
            fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle

    def restore(self, source):
        """Restore every declared tree that has a matching entry; returns the records (never raises)."""
        try:
            return self._restore(Path(source))
        except Exception as error:   # a cache fault never fails an attempt
            self.entries = []
            self.records = [dict(outcome='error', reason='%s: %s' % (type(error).__name__, str(error)[:200]))]
            return self.records

    def _restore(self, source):
        components, reason = load_manifest(source)
        if not components:
            self.records = [dict(outcome='skipped', reason=reason)]
            return self.records
        expanded, reason = expand(source, components)
        if expanded is None:
            self.records = [dict(outcome='skipped', reason=reason)]
            return self.records
        refresh = self._refresh_due()
        for path, key_paths in expanded:
            started = time.monotonic()
            record = dict(path=path)
            self.records.append(record)
            destination = source/path
            parent_chain = [source/PurePosixPath(path).parents[i] for i in range(len(PurePosixPath(path).parents) - 1)]
            if any(parent.is_symlink() for parent in parent_chain):
                record.update(outcome='skipped', reason='symlinked-parent')
                continue
            if os.path.lexists(destination):
                record.update(outcome='skipped', reason='destination-exists')
                continue
            key = entry_key(self.config, self.owner, self.tool, path, source, key_paths)
            if key is None:
                record.update(outcome='skipped', reason='no-key')
                continue
            record['key'] = key[:12]
            self.entries.append((path, key_paths, key, None))
            index = len(self.entries) - 1
            if refresh:
                record.update(outcome='refresh', reason='scheduled-cold-install')
                self.entries[index] = (path, key_paths, key, 'refresh')
                continue
            entry = self.root/'entries'/key
            meta = _read_json(entry/'meta.json')
            if meta is None or not (entry/'data').is_dir():
                record.update(outcome='miss', reason='no-entry')
                self.entries[index] = (path, key_paths, key, 'miss')
                continue
            lock = self._lock(False)
            if lock is None:
                record.update(outcome='miss', reason='busy')
                self.entries[index] = (path, key_paths, key, 'miss-busy')
                continue
            try:
                _copy(entry/'data', destination)
                files, size, error = scan(destination)
                if error or files != meta.get('files') or size != meta.get('bytes'):
                    _remove(destination)
                    record.update(outcome='miss', reason='verify-failed: %s' % (error or 'size-mismatch'))
                    self.entries[index] = (path, key_paths, key, 'miss')
                    _remove(entry)    # a damaged entry is dropped, never served
                    continue
                os.utime(entry/'meta.json')
            except Exception as error:
                _remove(destination)
                record.update(outcome='miss', reason='copy-failed: %s' % str(error)[:120])
                self.entries[index] = (path, key_paths, key, 'miss')
                continue
            finally:
                lock.close()
            record.update(outcome='reused', bytes=meta['bytes'], seconds=round(time.monotonic() - started, 2))
            self.entries[index] = (path, key_paths, key, 'reused')
        return self.records

    def reused(self):
        """The trees the handler may skip installing."""
        return [dict(path=record['path'], outcome='reused') for record in self.records if record.get('outcome') == 'reused']

    # -- handshake: the handler asks for the restore when ITS tree is ready
    # A job's source is often not the handler's working tree until the handler has
    # materialised it (Benchday's ZZOPS archive is app/ plus sibling dependency
    # groups, merged into one tree, and any extra file in the raw layout is refused).
    # So the worker restores on request, from the root the handler names, which must
    # lie inside the attempt's source directory. The handler only ever sees directories.
    def serve(self, handshake, boundary):
        """Poll for the handler's request; restore and answer it once. Never raises."""
        try:
            if self.served:
                return
            request = _read_json(Path(handshake)/REQUEST_FILE)
            if request is None:
                return
            self.served = True
            answer = dict(version=SCHEMA, components=[], outcome='skipped')
            root = request.get('root') if request.get('version') == SCHEMA else None
            try:
                real = os.path.realpath(root) if isinstance(root, str) else None
                inside = real is not None and os.path.commonpath([real, os.path.realpath(boundary)]) == os.path.realpath(boundary)
            except ValueError:
                inside = False
            if not inside or not os.path.isdir(real):
                self.records = [dict(outcome='skipped', reason='root-outside-source')]
            else:
                self.source = Path(real)
                self.restore(self.source)
                answer.update(outcome='restored', components=self.reused())
            _write_json(Path(handshake)/RESPONSE_FILE, answer)
        except Exception as error:
            self.records = [dict(outcome='error', reason='%s: %s' % (type(error).__name__, str(error)[:200]))]
            try:
                _write_json(Path(handshake)/RESPONSE_FILE, dict(version=SCHEMA, components=[], outcome='error'))
            except Exception:
                pass

    # -- save
    def save(self, source, output):
        """Store the trees the handler committed that were not restored; returns the records (never raises).

        `source` is only the fallback: when the handler asked for a restore, its root is used."""
        try:
            return self._save(self.source or Path(source), Path(output))
        except Exception as error:
            return [dict(outcome='error', reason='%s: %s' % (type(error).__name__, str(error)[:200]))]

    def _committed(self, output):
        value = _read_json(Path(output)/COMMIT_FILE)
        if (value is None or value.get('version') != SCHEMA or not isinstance(value.get('paths'), list) or
                len(value['paths']) > MAX_ENTRIES or not all(isinstance(item, str) for item in value['paths'])):
            return None
        return set(value['paths'])

    def _save(self, source, output):
        todo = [entry for entry in self.entries if entry[3] in ('miss', 'refresh')]
        if not todo:
            return []
        committed = self._committed(output)
        if committed is None:
            return [dict(path=entry[0], outcome='not-saved', reason='handler-wrote-no-commit') for entry in todo]
        saved = []
        for path, key_paths, key, how in todo:
            started = time.monotonic()
            record = dict(path=path, key=key[:12])
            saved.append(record)
            if path not in committed:
                record.update(outcome='not-saved', reason='not-committed')
                continue
            tree = source/path
            if tree.is_symlink() or not tree.is_dir():
                record.update(outcome='not-saved', reason='missing')
                continue
            if entry_key(self.config, self.owner, self.tool, path, source, key_paths) != key:
                record.update(outcome='not-saved', reason='key-changed')
                continue
            files, size, error = scan(tree, source)
            if error:
                record.update(outcome='not-saved', reason=error)
                continue
            if size > self.config['max_component_bytes'] or size > self.config['max_bytes'] // 2:
                record.update(outcome='not-saved', reason='too-large', bytes=size)
                continue
            lock = self._lock(True)
            if lock is None:
                record.update(outcome='not-saved', reason='busy')
                continue
            try:
                self._store(tree, path, key, files, size)
                record.update(outcome='saved', bytes=size, seconds=round(time.monotonic() - started, 2))
                self._evict(keep=key)
            except Exception as error:
                record.update(outcome='not-saved', reason='store-failed: %s' % str(error)[:120])
            finally:
                lock.close()
        return saved

    def _store(self, tree, path, key, files, size):
        entries = self.root/'entries'
        entries.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = entries/('tmp-%s-%d' % (self.attempt[:12], os.getpid()))
        _remove(temporary)
        temporary.mkdir(mode=0o700)
        try:
            _copy(tree, temporary/'data')
            copied = scan(temporary/'data')
            if copied != (files, size, None):
                raise RuntimeError('copy differs from source')
            _write_json(temporary/'meta.json', dict(schema=SCHEMA, path=path, files=files, bytes=size,
                                                    created=time.time(), tool=self.tool))
            final = entries/key
            if final.exists():
                old = entries/('tmp-old-%s-%d' % (key[:12], os.getpid()))
                os.rename(final, old)
                _remove(old)
            os.rename(temporary, final)
        except BaseException:
            _remove(temporary)
            raise

    def _evict(self, keep):
        """Bound enforcer: remove stale temporaries, then least recently used entries until under max_bytes."""
        entries = self.root/'entries'
        now = time.time()
        sized = []
        for item in entries.iterdir():
            if item.name.startswith('tmp-'):
                if now - item.stat().st_mtime > TEMP_AGE:
                    _remove(item)
                continue
            meta = _read_json(item/'meta.json')
            if meta is None:
                _remove(item)
                continue
            sized.append((os.stat(item/'meta.json').st_mtime, item, meta['bytes']))
        total = sum(size for _, _, size in sized)
        for _, item, size in sorted(sized, key=lambda row: row[0]):
            if total <= self.config['max_bytes']:
                break
            if item.name == keep:
                continue
            _remove(item)
            total -= size

    def outcome(self, saved):
        return dict(outcome='enabled', restored=self.records[:RECORDS], saved=saved[:RECORDS])
