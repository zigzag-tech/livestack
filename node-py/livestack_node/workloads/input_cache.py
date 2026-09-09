"""One worker's bounded source cache; used under its existing journal lock.

Entries are principal-scoped and digest-verified on every hit. The worker only
prunes while idle, after restart reconciliation. Retained inputs never evict.
None retention disables eviction/expiry; capacity exhaustion refuses admission.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from .archive import file_digest
from .blobs import BlobStore
from .model import WorkloadError, encode, name
from .transfer import InputTransfer


class InputCache:
    def __init__(self, root, transfer, *, max_bytes, max_entries=32, retention_seconds=14*86400, mirror=None):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise WorkloadError('input cache requires a positive byte limit')
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or not 1 <= max_entries <= 32:
            raise WorkloadError('input cache entry limit must be 1..32')
        if retention_seconds is not None and (isinstance(retention_seconds, bool) or
                not isinstance(retention_seconds, (int, float)) or not math.isfinite(retention_seconds) or retention_seconds <= 0):
            raise WorkloadError('cache retention must be positive or disabled')
        self.root = Path(root)
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise WorkloadError('cache must be a private directory')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.max_bytes, self.max_entries, self.retention = max_bytes, max_entries, retention_seconds
        self.transfer = InputTransfer(transfer.client, max_bytes=min(max_bytes, transfer.max_bytes))
        self.mirror = mirror
        self.index = self.root/'index.json'

    def _load(self):
        if not self.index.exists():
            return {}
        if self.index.is_symlink() or self.index.stat().st_size > 65536:
            raise WorkloadError('cache index exceeds bound or is not private')
        rows = json.loads(self.index.read_text())
        if not isinstance(rows, dict) or len(rows) > 32:
            raise WorkloadError('invalid cache index')
        for key, row in rows.items():
            if not re.fullmatch('[a-f0-9]{64}', key) or not isinstance(row, dict):
                raise WorkloadError('invalid cache entry')
            BlobStore.digest(row.get('digest'))
            if (isinstance(row.get('size'), bool) or not isinstance(row.get('size'), int) or not 0 <= row['size'] <= self.max_bytes or
                    not isinstance(row.get('used'), (int, float)) or not math.isfinite(row['used']) or not isinstance(row.get('retain'), bool)):
                raise WorkloadError('invalid cache metadata')
            path = self.root/key
            if path.is_symlink() or not path.is_file() or path.stat().st_size != row['size']:
                raise WorkloadError('cache entry differs from its inventory')
        if sum(row['size'] for row in rows.values()) > self.max_bytes or len(rows) > self.max_entries:
            raise WorkloadError('existing cache exceeds configured bounds')
        return rows

    def _save(self, rows):
        temporary = self.root/'index.tmp'
        with temporary.open('w') as stream:
            stream.write(encode(rows))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.index)
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _recover_orphans(self, rows):
        # Called only while this worker owns its journal lock and no attempt is
        # executing. A killed transfer may leave an uncommitted private file.
        for path in self.root.iterdir():
            if path.name in rows or path.name == 'index.json':
                continue
            if not (re.fullmatch('[a-f0-9]{64}', path.name) or path.name.startswith('.download-') or path.name == 'index.tmp'):
                raise WorkloadError('unrecognized file in input cache')
            if path.is_symlink() or not path.is_file():
                raise WorkloadError('non-private cache staging entry')
            path.unlink()

    def prune(self):
        rows = self._load()
        self._recover_orphans(rows)
        if self.retention is None:
            return
        now = time.time()
        for key, row in list(rows.items()):
            if not row['retain'] and now-row['used'] > self.retention:
                # Commit removal before unlink: a crash leaves a removable
                # orphan, never an index pointing at a deliberately missing file.
                del rows[key]
                self._save(rows)
                (self.root/key).unlink()

    def get(self, assignment):
        spec = assignment['spec']
        digest = BlobStore.digest(spec['input_digest'])
        owner = name(assignment['owner'], 'owner')
        key = hashlib.sha256((owner+'\0'+digest).encode()).hexdigest()
        rows = self._load()
        self._recover_orphans(rows)
        if key in rows:
            if rows[key]['digest'] != digest or file_digest(self.root/key) != digest:
                raise WorkloadError('cached source digest mismatch', 409)
            rows[key].update(used=time.time(), retain=rows[key]['retain'] or spec.get('retain', False))
            self._save(rows)
            return self.root/key
        # Reserve the maximum accepted transfer, including staging, before any
        # bytes arrive. Conservative reservation avoids transient quota excess.
        reserve = self.transfer.max_bytes
        used = sum(row['size'] for row in rows.values())
        candidates = sorted((key for key in rows if not rows[key]['retain']), key=lambda key: rows[key]['used'])
        while len(rows) >= self.max_entries or used+reserve > self.max_bytes:
            if self.retention is None or not candidates:
                raise WorkloadError('input cache capacity exhausted by protected content', 429)
            old = candidates.pop(0)
            used -= rows.pop(old)['size']
            self._save(rows)
            (self.root/old).unlink()
        path = None
        if self.mirror:
            try:
                path = self.mirror.get(digest, self.root/key, self.transfer.max_bytes)
            except WorkloadError:
                # Provider outages and corrupt mirror bytes never weaken the
                # attempt-scoped authority fallback or its final digest check.
                path = None
        if path is None:
            path = self.transfer.get(digest, self.root/key, assignment=assignment)
        rows[key] = dict(digest=digest, size=path.stat().st_size, used=time.time(), retain=spec.get('retain', False))
        self._save(rows)
        return path
