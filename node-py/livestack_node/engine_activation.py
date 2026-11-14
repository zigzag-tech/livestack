"""Operator-only, leased source/pin replacement. Does not load an engine."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import urllib.request
import urllib.error

MAX_JSON = 4 * 1024 * 1024


def read(path, limit=MAX_JSON, *, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('input must be a regular file')
        owners = {0, os.getuid()}
        sudo_uid = os.environ.get('SUDO_UID', '')
        if os.getuid() == 0 and sudo_uid.isdecimal():
            owners.add(int(sudo_uid))
        if private and (info.st_uid not in owners or info.st_mode & 0o077):
            raise ValueError('credential must be an owned private file')
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('input exceeds its byte bound')
    return raw


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def atomic(path, raw, mode=0o600, uid=None, gid=None):
    path = Path(path)
    temporary = path.with_name(path.name + '.activation-tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as f:
            if uid is not None:
                os.fchown(f.fileno(), uid, gid)
            os.fchmod(f.fileno(), mode)
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            timeout=20, check=True)
    if len(result.stdout) > MAX_JSON:
        raise ValueError('git output exceeds its bound')
    return result.stdout.decode().strip()


def plan(args):
    if not re.fullmatch(r'harmony-llm(?:-[a-zA-Z0-9_-]+)?\.service', args.service):
        raise ValueError('only an explicit harmony-llm facade service may restart')
    if not re.fullmatch('[0-9a-f]{40}', args.source_rev):
        raise ValueError('source revision must be a full git SHA')
    source = Path(args.source_root).resolve(strict=True)
    if git(source, 'rev-parse', 'HEAD') != args.source_rev:
        raise ValueError('source checkout differs from candidate revision')
    if git(source, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('candidate tracked source is dirty')
    if git(source, 'ls-files', '--others', '--exclude-standard', '--', 'serve', 'src', 'include'):
        raise ValueError('candidate contains untracked runtime source')
    if not (source / 'serve/server.py').is_file():
        raise ValueError('candidate has no Strata server')
    python = Path(args.python)
    if not python.is_absolute() or not os.access(python, os.X_OK):
        raise ValueError('candidate interpreter must be an absolute executable')
    marker = source / 'STRATA_VERSION'
    if marker.exists() and read(marker, 128).decode().strip() != args.source_rev:
        raise ValueError('candidate source marker differs from revision')
    venv = source / '.venv'
    if venv.exists() and venv.resolve() != python.parent.parent.resolve():
        raise ValueError('candidate interpreter differs from selected virtualenv')
    manifest_path = Path(args.artifacts).resolve(strict=True)
    manifest = json.loads(read(manifest_path, 65536))
    source_pin = manifest.get('source', '')
    if not isinstance(source_pin, str) or len(source_pin) < 8 or not args.source_rev.startswith(source_pin):
        raise ValueError('native artifact source differs from candidate')
    artifact_root = manifest_path.parent
    if not os.access(artifact_root / 'strata', os.X_OK):
        raise ValueError('native engine is not executable')
    for name in ('strata', 'libstrata_kq.so'):
        item = manifest['files'][name]
        p = artifact_root / name
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != item['bytes'] or not 0 < info.st_size <= 2 * 1024**3:
                raise ValueError('native artifact size/type differs')
            h = hashlib.sha256()
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        if h.hexdigest() != item['sha256']:
            raise ValueError('native artifact digest differs')
    units_path = Path(args.units_file)
    if not units_path.is_absolute():
        raise ValueError('unit declaration must use an absolute path')
    original = read(units_path)
    if digest(original) != args.expected_units_sha256:
        raise ValueError('declaration digest changed')
    declarations = json.loads(original)
    units = declarations if isinstance(declarations, list) else declarations['units']
    if not isinstance(units, list) or len(units) > 64:
        raise ValueError('invalid or oversized unit list')
    matches = [u for u in units if u.get('name') == args.unit]
    if len(matches) != 1 or matches[0].get('engine') != 'strata':
        raise ValueError('exactly one existing Strata unit must match')
    unit = matches[0]
    if unit.get('engine_source', {}).get('rev') != args.expected_source_rev:
        raise ValueError('declared source pin changed')
    run = json.loads(read(unit['strata_config']))
    run['exe'] = str(artifact_root / 'strata')
    run['lib_dirs'] = list(dict.fromkeys([str(artifact_root)] + run.get('lib_dirs', [])))
    candidate = copy.deepcopy(declarations)
    selected = next(u for u in (candidate if isinstance(candidate, list) else candidate['units']) if u['name'] == args.unit)
    selected['strata_root'] = str(source)
    selected['strata_config'] = str(source / 'operator-runtime.json')
    selected.setdefault('engine_source', {})['rev'] = args.source_rev
    # Existing model identity and all placement/residency declarations remain intact.
    return {'original': original, 'candidate': encoded(candidate), 'run': encoded(run),
            'source': source, 'python': python, 'units_path': units_path,
            'source_rev': args.source_rev, 'unit': args.unit}


class DeviceLease:
    """Existing host-broker exclusive admission, with bounded, explicit release."""
    def __init__(self, args):
        self.args, self.stop, self.failure, self.lease_id = args, threading.Event(), None, None
        from urllib.parse import urlsplit
        url = urlsplit(args.broker_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('broker URL must contain no credentials or parameters')
        self.token = read(args.token_file, 2048, private=True).decode().strip()
        if not self.token or any(c.isspace() for c in self.token):
            raise ValueError('invalid private broker credential')

    def post(self, path, body, timeout=10):
        req = urllib.request.Request(self.args.broker_url.rstrip('/') + path,
            data=encoded(body), headers={'authorization': 'Bearer ' + self.token,
                                        'content-type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(65537)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'broker request refused: HTTP {exc.code}') from None
        if len(raw) > 65536:
            raise ValueError('broker response exceeds 64 KiB')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('broker response is not an object')
        return result

    def __enter__(self):
        a = self.args
        value = self.post('/admit', {'kind': a.unit, 'owner': a.owner,
            'exclusive_device': True, 'selector': {'device_id': a.device_id},
            'min_free_bytes': a.min_free_bytes, 'wait_timeout_s': a.wait_seconds,
            'wait_s': a.wait_seconds}, a.wait_seconds + 30)
        if (value.get('granted') is not True or value.get('degraded')
                or value.get('exclusive_device') is not True or value.get('state') != 'exclusive'
                or value.get('device_id') != a.device_id
                or not isinstance(value.get('lease_id'), str) or not value['lease_id']):
            raise RuntimeError('exclusive admission refused: ' + str(value.get('reason', 'invalid grant'))[:256])
        self.lease_id = value['lease_id']
        if len(self.lease_id) > 256:
            raise ValueError('lease identity exceeds bound')
        def beat():
            while not self.stop.wait(20):
                try:
                    if self.post(self.path('heartbeat'), {'ttl_seconds': 120}).get('ok') is not True:
                        raise RuntimeError('lease heartbeat refused')
                except Exception:
                    self.failure = 'lease heartbeat unconfirmed'
                    return
        self.thread = threading.Thread(target=beat, daemon=True)
        self.thread.start()
        return self

    def path(self, action):
        from urllib.parse import quote
        return '/lease/' + quote(self.lease_id, safe='') + '/' + action

    def check(self):
        if self.failure:
            raise RuntimeError(self.failure)
        if self.post(self.path('heartbeat'), {'ttl_seconds': 120}).get('ok') is not True:
            raise RuntimeError('lease ownership unconfirmed')

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=12)
        if self.thread.is_alive() or self.post(self.path('release'), {}).get('ok') is not True:
            raise RuntimeError('exclusive lease release unconfirmed')


def restart(service):
    if not re.fullmatch(r'harmony-llm(?:-[a-zA-Z0-9_-]+)?\.service', service):
        raise ValueError('only an explicit harmony-llm facade service may restart')
    subprocess.run(['systemctl', 'restart', service], check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['systemctl', 'is-active', '--quiet', service], check=True, timeout=10)


def apply(args, *, lease_factory=DeviceLease, restart_service=restart):
    validated = plan(args)
    operation = Path(args.operation_dir)
    # A dedicated receipt directory holds at most eight operations; refuse
    # rather than silently deleting retained deployment evidence.
    if not operation.parent.is_dir():
        raise ValueError('operation parent must be an existing dedicated directory')
    with os.scandir(operation.parent) as entries:
        for i, _ in enumerate(entries):
            if i >= 7:
                raise ValueError('operation receipt count bound reached; retain/export before retry')
    operation.mkdir(mode=0o700)  # exclusive-create; no unbounded append or retry history
    journal = {'source_rev': args.source_rev, 'unit': args.unit,
               'before_sha256': digest(validated['original']),
               'candidate_sha256': digest(validated['candidate']), 'phase': 'validated'}
    def record(phase):
        journal['phase'] = phase
        raw = encoded(journal)
        if len(raw) > 65536:
            raise ValueError('activation journal exceeds 64 KiB')
        atomic(operation / 'journal.json', raw)
    record('validated')
    atomic(operation / 'previous-units.json', validated['original'])
    path = validated['units_path']
    lock_fd = os.open(str(path) + '.activation-lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with lease_factory(args) as lease:
            journal['lease_id'] = lease.lease_id
            record('leased')
            # Admission may wait; neither an earlier plan nor its lock proves current inputs.
            current = plan(args)
            lease.check()
            source = current['source']
            if not (source / '.venv').exists():
                (source / '.venv').symlink_to(current['python'].parent.parent)
            if not (source / 'STRATA_VERSION').exists():
                atomic(source / 'STRATA_VERSION', (args.source_rev + '\n').encode(), 0o644)
            # The operator may be root while the facade runs as the checkout's
            # service owner. Keep the private runtime readable by that owner.
            source_owner = source.stat()
            atomic(source / 'operator-runtime.json', current['run'],
                   uid=source_owner.st_uid, gid=source_owner.st_gid)
            info = path.stat()
            if read(path) != current['original']:
                raise RuntimeError('declaration changed before selection')
            atomic(path, current['candidate'], stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
            record('selected')
            try:
                lease.check()
                restart_service(args.service)
                lease.check()
                record('active')
            except Exception:
                if digest(read(path)) != journal['candidate_sha256']:
                    record('rollback_refused_concurrent_edit')
                    raise RuntimeError('rollback refused: another declaration owns the file') from None
                atomic(path, current['original'], stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
                record('restored')
                try:
                    restart_service(args.service)
                except Exception:
                    record('rollback_restart_unconfirmed')
                    raise RuntimeError('previous service restart unconfirmed') from None
                record('rolled_back')
                raise RuntimeError('activation failed; previous declaration restored') from None
        record('completed')
    except Exception:
        if journal['phase'] not in ('rolled_back', 'rollback_restart_unconfirmed', 'rollback_refused_concurrent_edit'):
            record('failed_after_' + journal['phase'])
        raise
    finally:
        os.close(lock_fd)
    return journal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('units-file', 'unit', 'expected-source-rev', 'expected-units-sha256',
                 'source-root', 'source-rev', 'artifacts', 'python'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--operation-dir')
    parser.add_argument('--broker-url')
    parser.add_argument('--token-file')
    parser.add_argument('--device-id')
    parser.add_argument('--owner')
    parser.add_argument('--service', default='harmony-llm.service')
    parser.add_argument('--wait-seconds', type=int, default=120)
    parser.add_argument('--min-free-bytes', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.wait_seconds <= 480 or not 1 <= args.min_free_bytes <= 1024**4:
        parser.error('wait or memory bound is invalid')
    if args.apply and not all((args.operation_dir, args.broker_url, args.token_file, args.device_id, args.owner)):
        parser.error('apply requires operation directory, broker, private token file, device and owner')
    try:
        if args.apply:
            value = apply(args)
        else:
            p = plan(args)
            value = {'phase': 'plan', 'source_rev': args.source_rev,
                     'before_sha256': digest(p['original']), 'candidate_sha256': digest(p['candidate'])}
        print(json.dumps({'ok': True, **value}))
        return 0
    except Exception as exc:
        # HTTP/subprocess exception text may contain private response/configuration values.
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__,
                          'detail': str(exc)[:512] if isinstance(exc, (ValueError, RuntimeError)) else 'operator activation failed; inspect private journal'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
