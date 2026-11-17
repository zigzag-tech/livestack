"""Owner-bound same-host artifact grants and a private bounded content cache."""
import hashlib
import json
import math
from itertools import islice
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager

from .model import WorkloadError, encode, name

MAX_GRANTS = 4096
MAX_TTL = 86400
REFERENCE_KEYS = {'version', 'job_id', 'attempt_id', 'fence', 'source_digest', 'owner',
                  'host', 'store', 'handle', 'digest', 'size', 'expires'}
HEX = re.compile('[a-f0-9]{64}')
HANDLE = re.compile('[a-f0-9]{32}')


def register(store, worker, boot, attempt_id, fence, artifact):
    if not isinstance(artifact, dict) or set(artifact) != {'name', 'digest', 'size', 'store', 'handle', 'ttl_seconds'}:
        raise WorkloadError('invalid local artifact registration')
    for field in ('name', 'store'):
        name(artifact[field], 'local artifact '+field)
    if not isinstance(artifact['digest'], str) or not HEX.fullmatch(artifact['digest']):
        raise WorkloadError('invalid local artifact digest')
    if not isinstance(artifact['handle'], str) or not HANDLE.fullmatch(artifact['handle']):
        raise WorkloadError('invalid local artifact handle')
    size, ttl = artifact['size'], artifact['ttl_seconds']
    if type(size) is not int or not 0 <= size <= 8*1024**3 or type(ttl) is not int or not 60 <= ttl <= MAX_TTL:
        raise WorkloadError('invalid local artifact size or lifetime')
    now = store.clock()
    with store.transaction() as db:
        store._expire(db, now)
        store._worker(db, worker, boot)
        attempt = db.execute('SELECT * FROM attempts WHERE id=? AND worker=? AND boot=? AND fence=?',
                             (attempt_id, worker, boot, fence)).fetchone()
        if not attempt:
            raise WorkloadError('local artifact attempt unavailable', 409)
        job = store._job(db, attempt['job'])
        report = json.loads(db.execute('SELECT report FROM workers WHERE id=?', (worker,)).fetchone()[0])
        delivery = job['spec'].get('delivery') or {}
        if (attempt['state'] != 'running' or attempt['expires'] <= now or job['state'] != 'running' or
                job['fence'] != fence or delivery.get('consumer_host') != attempt['host'] or
                report.get('local_artifacts') is not True):
            raise WorkloadError('local artifact handoff not authorized for this attempt', 409)
        reference = dict(version=1, job_id=job['id'], attempt_id=attempt_id, fence=fence,
                         source_digest=job['spec']['input_digest'], owner=job['owner'], host=attempt['host'],
                         store=artifact['store'], handle=artifact['handle'], digest=artifact['digest'],
                         size=size, expires=now+ttl)
        db.execute('DELETE FROM local_artifacts WHERE expires<=?', (now,))
        old = db.execute('SELECT body FROM local_artifacts WHERE handle=?', (artifact['handle'],)).fetchone()
        if old:
            previous = json.loads(old['body'])
            if any(previous[k] != reference[k] for k in REFERENCE_KEYS-{'expires'}):
                raise WorkloadError('local artifact handle has different bindings', 409)
            return previous
        if db.execute('SELECT count(*) FROM local_artifacts').fetchone()[0] >= MAX_GRANTS:
            raise WorkloadError('local artifact grant capacity exhausted', 429)
        db.execute('INSERT INTO local_artifacts(handle,job,attempt,digest,expires,body) VALUES(?,?,?,?,?,?)',
                   (reference['handle'], job['id'], attempt_id, reference['digest'], reference['expires'], encode(reference)))
        return reference


def read_reference(store, owner, job_id, digest):
    from .shared_access import read_job
    now = store.clock()
    with store.transaction() as db:
        job = read_job(store, db, owner, job_id)
        if job['state'] != 'succeeded':
            raise WorkloadError('local artifact job is not succeeded', 409)
        artifacts = ((job.get('result') or {}).get('result') or {}).get('artifacts', [])
        matches = [a for a in artifacts if a.get('digest') == digest and a.get('local')]
        if not matches:
            raise WorkloadError('local artifact not found', 404)
        reference = matches[0]['local']
        row = db.execute('SELECT body FROM local_artifacts WHERE handle=? AND job=? AND digest=? AND expires>?',
                         (reference['handle'], job_id, digest, now)).fetchone()
        if not row or json.loads(row['body']) != reference:
            raise WorkloadError('local artifact grant expired or unavailable', 410)
        return reference


def validate_reference(db, owner, artifact, job_id, attempt_id, now):
    reference = artifact.get('local')
    if not isinstance(reference, dict) or set(reference) != REFERENCE_KEYS:
        raise WorkloadError('invalid local artifact reference')
    row = db.execute('SELECT body FROM local_artifacts WHERE handle=? AND job=? AND attempt=? AND digest=? AND expires>?',
                     (reference['handle'], job_id, attempt_id, artifact['digest'], now)).fetchone()
    if (not row or json.loads(row['body']) != reference or reference['owner'] != owner or
            reference['size'] != artifact['size']):
        raise WorkloadError('local artifact is expired, foreign, or has different bindings', 409)


class LocalArtifactStore:
    """Disk-only cache. sweep (also called by report) enforces count/bytes/age.

    At most 64 live handles and 2 GiB by default. Unknown files/symlinks fail
    closed. Clients use operator-configured roots, never paths from a worker.
    """
    def __init__(self, root, host, store_id, *, max_bytes=2*1024**3, max_handles=64, ttl_seconds=3600, clock=time.time, create=True):
        self.root = Path(root)
        if not self.root.is_absolute() or self.root.resolve() != self.root:
            raise WorkloadError('local artifact root must be an absolute non-symlink path')
        self.host, self.store_id = name(host, 'local artifact host'), name(store_id, 'local artifact store')
        if (type(max_bytes) is not int or not 1 <= max_bytes <= 8*1024**3 or
                type(max_handles) is not int or not 1 <= max_handles <= 128 or
                type(ttl_seconds) is not int or not 60 <= ttl_seconds <= MAX_TTL):
            raise WorkloadError('invalid local artifact storage bounds')
        self.max_bytes, self.max_handles, self.ttl_seconds, self.clock = max_bytes, max_handles, ttl_seconds, clock
        for path in (self.root, self.root/'objects', self.root/'handles'):
            if create:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not path.exists():
                raise WorkloadError('local_artifact_unavailable: private cache is absent', 404)
            metadata = path.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                raise WorkloadError('local artifact store must be private and owned')

    def _metadata(self, path):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
            raise WorkloadError('invalid local artifact metadata')
        try:
            return json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            raise WorkloadError('local artifact metadata is unreadable', 409) from error

    @contextmanager
    def _locked(self):
        import fcntl
        fd = os.open(self.root/'.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _write(self, reference):
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix='.meta-')
        try:
            with os.fdopen(fd, 'w') as outgoing:
                outgoing.write(encode(reference))
            os.replace(temporary, self.root/'handles'/reference['handle'])
        finally:
            Path(temporary).unlink(missing_ok=True)

    def sweep(self):
        with self._locked():
            return self._sweep()

    def _sweep(self):
        for pattern in ('.put-*', '.meta-*'):
            staging = list(islice(self.root.glob(pattern), self.max_handles+1))
            if len(staging) > self.max_handles:
                raise WorkloadError('local artifact staging inventory exceeds bound')
            for path in staging:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise WorkloadError('invalid local artifact staging file')
                path.unlink()
        handles = list(islice((self.root/'handles').iterdir(), self.max_handles+1))
        if len(handles) > self.max_handles:
            raise WorkloadError('local artifact handle inventory exceeds bound')
        live = set()
        for path in handles:
            if not HANDLE.fullmatch(path.name):
                raise WorkloadError('unknown local artifact handle file')
            reference = self._metadata(path)
            if reference['expires'] <= self.clock():
                path.unlink()
            else:
                live.add(reference['digest'])
        objects = list(islice((self.root/'objects').iterdir(), self.max_handles+1))
        if len(objects) > self.max_handles:
            raise WorkloadError('local artifact object inventory exceeds bound')
        size = 0
        for path in objects:
            if not HEX.fullmatch(path.name) or not stat.S_ISREG(path.lstat().st_mode):
                raise WorkloadError('invalid local artifact object')
            if path.name not in live:
                path.unlink()
            else:
                size += path.stat().st_size
        if size > self.max_bytes:
            raise WorkloadError('local artifact byte inventory exceeds bound')
        return size

    def put(self, source, assignment):
        with self._locked():
            return self._put(source, assignment)

    def _put(self, source, assignment):
        metadata = Path(source).lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkloadError('local artifact source must be a regular non-symlink file')
        size = metadata.st_size
        used = self._sweep()
        if size > self.max_bytes-used or len(list((self.root/'handles').iterdir())) >= self.max_handles:
            raise WorkloadError('local artifact cache capacity exhausted', 429)
        handle = secrets.token_hex(16)
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix='.put-')
        digest, copied = hashlib.sha256(), 0
        try:
            with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as incoming, os.fdopen(fd, 'wb') as outgoing:
                for block in iter(lambda: incoming.read(65536), b''):
                    copied += len(block)
                    if copied > size:
                        raise WorkloadError('local artifact changed during copy')
                    outgoing.write(block); digest.update(block)
            if copied != size:
                raise WorkloadError('local artifact changed during copy')
            hexdigest = digest.hexdigest()
            target = self.root/'objects'/hexdigest
            if not target.exists():
                os.replace(temporary, target)
            reference = dict(version=1, job_id=assignment['job_id'], attempt_id=assignment['attempt_id'],
                             fence=assignment['fence'], source_digest=assignment['spec']['input_digest'],
                             owner=assignment['owner'], host=self.host, store=self.store_id, handle=handle,
                             digest=hexdigest, size=size, expires=self.clock()+self.ttl_seconds)
            self._write(reference)
            return reference
        finally:
            Path(temporary).unlink(missing_ok=True)

    def bind(self, reference):
        with self._locked():
            self._verify(reference)
            self._write(reference)

    def _verify(self, reference):
        if (not isinstance(reference, dict) or set(reference) != REFERENCE_KEYS or
                reference['host'] != self.host or reference['store'] != self.store_id or
                not isinstance(reference['handle'], str) or not HANDLE.fullmatch(reference['handle']) or
                not isinstance(reference['digest'], str) or not HEX.fullmatch(reference['digest']) or
                type(reference['expires']) not in (int, float) or not math.isfinite(reference['expires']) or
                reference['expires'] <= self.clock()):
            raise WorkloadError('local artifact binding unavailable or expired', 410)
        local = self._metadata(self.root/'handles'/reference['handle'])
        if any(local[k] != reference[k] for k in REFERENCE_KEYS-{'expires'}):
            raise WorkloadError('local artifact binding differs', 409)

    def copy(self, reference, destination):
        with self._locked():
            return self._copy(reference, destination)

    def _copy(self, reference, destination):
        self._verify(reference)
        path = self.root/'objects'/reference['digest']
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        target = Path(destination)
        out_fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.local-artifact-')
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, 'rb') as incoming, os.fdopen(out_fd, 'wb') as outgoing:
                metadata = os.fstat(incoming.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != reference['size']:
                    raise WorkloadError('local artifact size or type differs', 409)
                for block in iter(lambda: incoming.read(65536), b''):
                    size += len(block)
                    if size > reference['size']:
                        raise WorkloadError('local artifact exceeds declared size', 409)
                    outgoing.write(block); digest.update(block)
            if size != reference['size'] or digest.hexdigest() != reference['digest']:
                raise WorkloadError('local artifact digest differs', 409)
            self._verify(reference)
            os.link(temporary, target)  # Never overwrite a caller's existing file.
            return target
        finally:
            Path(temporary).unlink(missing_ok=True)
