"""Persistent, bounded, per-principal Docker data root for rootless attempts.

Design: openspec/changes/docker-build-cache, operator doc: node-py/docs/docker-build-cache.md.

Two halves. The worker side (`settings`, `plan`, `read_outcome`) runs in the
host user namespace and only validates config and hands a plan to the attempt.
The attempt side (`Session`) runs inside the RootlessKit namespace in
docker_command.py, where uid 0 maps over the sub-uid files dockerd creates, so
only it can measure and delete the root. Nothing here may fail an attempt: every
entry point that the launcher calls catches its own faults and returns the
ephemeral (today's) behaviour with the reason named in the outcome.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

SCHEMA = 1
KEYS = {'enabled', 'path', 'max_bytes', 'epoch', 'canary_every', 'max_growth'}
OUTCOME_FILE = 'docker-cache.json'
SESSION_FILE = 'docker-cache-session.json'
EXIT_FILE = 'docker-cache-exit.json'
FINGERPRINT_FILE = 'docker-cache-fingerprint.json'
MAX_FILE = 262144
RECORDS = 16


# ---------------------------------------------------------------- worker side

def settings(raw):
    """Validate worker.json `docker_cache`; returns (settings or None, reason).

    Missing, `enabled:false` and invalid all mean disabled; only an invalid
    block deserves a log line, which the caller emits from the reason.
    """
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
    def number(key, low, high, default=None):
        value = raw.get(key, default)
        whole = key != 'max_growth'
        if (isinstance(value, bool) or not isinstance(value, (int,) if whole else (int, float))
                or not low <= value <= high):
            raise ValueError('%s must be a number from %s to %s' % (key, low, high))
        return value
    try:
        out = dict(path=str(Path(path)), max_bytes=int(number('max_bytes', 64*1024**2, 4*1024**4)),
                   epoch=int(number('epoch', 0, 2**31, 0)), canary_every=int(number('canary_every', 0, 1000, 20)),
                   max_growth=float(number('max_growth', 1.0, 10.0, 3.0)))
    except (ValueError, KeyError, TypeError) as error:
        return None, 'invalid: %s' % error
    return out, 'enabled'


def namespace(owner):
    """Directory name for a principal: readable slug plus a digest so slugs cannot collide."""
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', str(owner))[:32].strip('.-') or 'owner'
    return '%s-%s' % (slug, hashlib.sha256(str(owner).encode()).hexdigest()[:12])


def plan(config, owner, attempt):
    """The JSON the attempt receives, or None when the cache is off."""
    return None if config is None else dict(config, namespace=namespace(owner), attempt=str(attempt))


def read_outcome(output):
    """The attempt's outcome record (bounded read), or None if it never wrote one."""
    path = Path(output)/OUTCOME_FILE
    try:
        if path.is_symlink() or path.stat().st_size > 16384:
            return None
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def purge(path, ns=None):
    """Delete a namespace (or the whole cache) from OUTSIDE the namespace, via rootlesskit like remove_data."""
    from .docker_runtime import run_in_userns
    target = Path(path)/ns if ns else Path(path)
    if not target.exists():
        return
    run_in_userns(['/usr/bin/rm', '-rf', '--', str(target)], timeout=600)


# ------------------------------------------------------------- attempt side

def _canary_due(plan_):
    every = plan_['canary_every']
    return bool(every) and int(hashlib.sha256(plan_['attempt'].encode()).hexdigest(), 16) % every == 0


def tree_bytes(root):
    """Disk usage (allocated blocks, hard links once) of a tree, never following symlinks.

    Returns (bytes, errors). A directory or entry that cannot be read is an ERROR,
    not zero: a walk that silently skipped a subtree would under-report the very
    size the bound is enforced on. Entries that vanish mid-walk are not errors.
    """
    total, errors, seen, stack = 0, 0, set(), [str(root)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except FileNotFoundError:
            continue
        except OSError:
            errors += 1
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError:
                errors += 1
                continue
            if info.st_nlink > 1 and not stat.S_ISDIR(info.st_mode):
                if (info.st_dev, info.st_ino) in seen:
                    continue
                seen.add((info.st_dev, info.st_ino))
            total += info.st_blocks * 512
            if stat.S_ISDIR(info.st_mode):
                stack.append(entry.path)
    return total, errors


def _wipe(path):
    subprocess.run(['/usr/bin/rm', '-rf', '--', str(path)], check=True, timeout=900,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _read_json(path):
    try:
        if Path(path).is_symlink() or Path(path).stat().st_size > MAX_FILE:
            return None
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path, value):
    tmp = Path(str(path)+'.tmp')
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)


class Session:
    """One attempt's relation to the persistent root. Never raises out of begin/finish."""

    def __init__(self, plan_, output):
        self.plan, self.output = plan_, Path(output)
        self.dir = Path(plan_['path'])/plan_['namespace'] if plan_ else None
        self.root = None            # persistent root in use, or None (ephemeral)
        self.lock_fd = None
        self.state = {}
        self.canary = False
        self.unclean = False        # dockerd had to be killed: the store may be inconsistent
        self.result = dict(outcome='disabled', reason='not-configured')
        self.started = time.monotonic()

    # -- begin ---------------------------------------------------------------
    def begin(self):
        """Return the persistent data root to use, or None for an ephemeral root."""
        if not self.plan:
            return None
        p = self.plan
        self.result = dict(outcome='cold-new', namespace=p['namespace'], epoch=p['epoch'])
        try:
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.dir.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return self._ephemeral('cold-unsafe', 'directory is not a private directory of this user')
            fd = os.open(self.dir/'slot.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                holder = (_read_json(self.dir/'holder.json') or {})
                return self._ephemeral('cold-locked', 'root in use by attempt %s' % holder.get('attempt', '?'))
            self.lock_fd = fd
            _write_json(self.dir/'holder.json', dict(attempt=p['attempt'], pid=os.getpid(), t=time.time()))
            self.state = _read_json(self.dir/'state.json') or {}
            slot = self.dir/'slot'
            reason = None
            if slot.is_symlink():
                reason = 'slot-is-symlink'
            elif slot.exists():
                if self.state.get('schema') != SCHEMA or self.state.get('namespace') != p['namespace']:
                    reason = 'identity-mismatch'
                elif self.state.get('epoch') != p['epoch']:
                    reason = 'epoch'
                elif self.state.get('clean') is not True:
                    reason = 'unclean-previous-exit'
            if reason:
                self._wipe_slot(reason)
            fresh = not slot.exists()
            if fresh:
                slot.mkdir(mode=0o700)
                self.state = dict(schema=SCHEMA, namespace=p['namespace'], epoch=p['epoch'], attempts=0)
            if self.state.get('attempts', 0) == 0 and not fresh:
                fresh = True
            # The deterministic cold canary runs on an ephemeral root; a fresh root IS the cold reference.
            if not fresh and _canary_due(p):
                self.canary = True
                self.state['clean'] = True
                self._release()
                self.result.update(outcome='cold-canary', reason='deterministic canary')
                return None
            self.state.update(clean=False, attempt=p['attempt'])
            _write_json(self.dir/'state.json', self.state)
            self.root = slot
            self.result.update(outcome='cold-new' if fresh else 'hit', cold_bytes=self.state.get('cold_bytes'))
            if reason:
                self.result.update(outcome='wiped', reason=reason)
            return slot
        except Exception as error:
            self._release()
            self.root = None
            return self._ephemeral('cold-error', '%s: %s' % (type(error).__name__, str(error)[:200]))

    def _ephemeral(self, outcome, reason):
        self.result.update(outcome=outcome, reason=reason)
        return None

    def _release(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)   # closing the fd drops the flock
            self.lock_fd = None

    def _wipe_slot(self, reason):
        self.result.update(outcome='wiped', reason=reason)
        _wipe(self.dir/'slot')
        for name in ('state.json', 'canary.json'):
            try:
                (self.dir/name).unlink()
            except FileNotFoundError:
                pass
        self.state = {}

    def start_failed(self):
        """dockerd would not start on the persistent root: wipe it; the caller retries ephemerally."""
        if self.root is None:
            return
        try:
            self._wipe_slot('dockerd-start-failed')
        except Exception as error:
            self.result['wipe_error'] = '%s: %s' % (type(error).__name__, str(error)[:200])
        self.root = None
        self._release()

    def announce(self):
        """Tell the native frontend (which prunes while dockerd is up) what applies."""
        try:
            _write_json(self.output/SESSION_FILE, dict(persistent=self.root is not None,
                                                       max_bytes=self.plan['max_bytes'] if self.plan else 0))
        except Exception:
            pass

    # -- finish --------------------------------------------------------------
    def finish(self, code=None):
        """After dockerd stopped: size guard, canary, state, outcome file. Never raises."""
        if not self.plan:
            return
        try:
            self._finish(code)
        except Exception as error:
            self.result['finish_error'] = '%s: %s' % (type(error).__name__, str(error)[:200])
            # The root's cleanliness is unknown: leave clean:false so the next attempt wipes it.
        finally:
            self._release()
            self.result['seconds'] = round(time.monotonic()-self.started, 1)
            try:
                _write_json(self.output/OUTCOME_FILE, self.result)
            except Exception:
                pass
            print('docker_cache: ' + json.dumps(self.result), flush=True)

    def _exit_code(self, code):
        if code is not None:
            return code
        record = _read_json(self.output/EXIT_FILE)
        return record.get('code') if record else None

    def _finish(self, code):
        p, code = self.plan, self._exit_code(code)
        if self.root is None and not self.canary:
            return
        exit_record = _read_json(self.output/EXIT_FILE) or {}
        if 'prune' in exit_record:
            self.result['prune'] = exit_record['prune']
        if self.unclean:
            self.result['dockerd_killed'] = True
        fingerprint = _read_json(self.output/FINGERPRINT_FILE) or {}
        mode = 'cold' if (self.canary or self.result.get('outcome') in ('cold-new', 'wiped')) else 'warm'
        mismatch = None
        locked = self.lock_fd is not None
        if self.canary:
            locked = self._try_lock()
        if locked:
            state = _read_json(self.dir/'state.json') or self.state
            records = (_read_json(self.dir/'canary.json') or {}).get('records', [])
            record = dict(mode=mode, attempt=p['attempt'], ok=code == 0, inputs=fingerprint.get('inputs'),
                          outputs=fingerprint.get('outputs'), t=int(time.time()))
            other = next((r for r in reversed(records) if r.get('mode') != mode and r.get('inputs') == record['inputs']), None)
            if other and (other.get('ok') != record['ok'] or
                          (other.get('outputs') is not None and record['outputs'] is not None and other['outputs'] != record['outputs'])):
                mismatch = dict(against=other.get('attempt'), this=mode, ok=[other.get('ok'), record['ok']])
            records = (records + [record])[-RECORDS:]
            if mismatch:
                self.result['canary_mismatch'] = mismatch
                print('docker_cache_canary_mismatch: ' + json.dumps(mismatch), flush=True)
                self._wipe_slot('canary-mismatch')
                self.result['outcome'] = 'wiped'
                self.root = None
            else:
                _write_json(self.dir/'canary.json', dict(records=records))
            if self.root is not None:
                nbytes, walk_errors = tree_bytes(self.root)
                self.result['bytes'] = nbytes
                if mode == 'cold' and state.get('cold_bytes') is None:
                    state['cold_bytes'] = nbytes
                if walk_errors or nbytes > p['max_bytes'] or (state.get('cold_bytes') and nbytes > p['max_growth']*state['cold_bytes']):
                    why = ('unmeasurable' if walk_errors else 'size' if nbytes > p['max_bytes'] else 'growth')
                    self.result['walk_errors'] = walk_errors
                    self._wipe_slot('discarded-'+why)
                    self.result.update(outcome='discarded', reason=why, cold_bytes=state.get('cold_bytes'))
                    self.root = None
                else:
                    state.update(clean=not self.unclean, bytes=nbytes, attempts=state.get('attempts', 0)+1, last_attempt=p['attempt'])
                    _write_json(self.dir/'state.json', state)
                    self.result['cold_bytes'] = state.get('cold_bytes')
        else:
            self.result['canary_skipped'] = 'root busy'

    def _try_lock(self):
        try:
            fd = os.open(self.dir/'slot.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self.lock_fd = fd
        return True


KEEP_FRACTION = 0.7   # builder cache is pruned to this share of max_bytes; the rest is headroom for images and one attempt's growth


def prune(env, max_bytes):
    """End-of-attempt prune while dockerd is up. Returns a small report; never raises."""
    report = {}
    def run(name, *argv, timeout=240):
        try:
            done = subprocess.run(['/usr/bin/docker', *argv], env=env, capture_output=True, text=True, timeout=timeout)
            report[name] = done.returncode if done.returncode else 'ok'
            return done
        except Exception as error:
            report[name] = type(error).__name__
    try:
        listing = subprocess.run(['/usr/bin/docker', 'ps', '-aq'], env=env, capture_output=True, text=True, timeout=30)
        ids = listing.stdout.split()
        if ids:
            run('rm', 'rm', '-f', '-v', *ids)
    except Exception as error:
        report['rm'] = type(error).__name__
    run('volume', 'volume', 'prune', '-f')
    run('network', 'network', 'prune', '-f')
    run('image', 'image', 'prune', '-f')
    run('builder', 'builder', 'prune', '-f', '--keep-storage', str(int(max_bytes * KEEP_FRACTION)))
    over = _docker_bytes(env)
    report['docker_bytes'] = over
    if over is not None and over > max_bytes:
        run('image_all', 'image', 'prune', '-a', '-f')
    return report


def _docker_bytes(env):
    try:
        done = subprocess.run(['/usr/bin/docker', 'system', 'df', '--format', '{{json .}}'], env=env,
                              capture_output=True, text=True, timeout=60)
        total = 0
        for line in done.stdout.splitlines():
            row = json.loads(line)
            total += _human(row.get('Size', '0B'))
        return int(total)
    except Exception:
        return None


def _human(text):
    match = re.fullmatch(r'\s*([0-9.]+)\s*([kKMGT]?i?B)\s*', str(text))
    if not match:
        return 0
    units = {'B': 1, 'kB': 1e3, 'KB': 1e3, 'MB': 1e6, 'GB': 1e9, 'TB': 1e12,
             'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3, 'TiB': 1024**4}
    return float(match.group(1)) * units.get(match.group(2), 1)
