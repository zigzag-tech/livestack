"""Bounded immutable content storage with principal-scoped access.

One digest names one byte sequence. Objects referenced by durable jobs cannot be
pruned. The authority serializes quota reservation; uploads stream to private
staging files rather than buffering whole source archives in memory.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import os
import threading
from pathlib import Path
import re
import time
import uuid

from .model import WorkloadError, name
from .blob_references import REFERENCE_DDL


class BlobStore:
    def __init__(self, store, root, *, max_bytes=200*1024**3, max_object_bytes=2*1024**3,
                 max_objects=20000, retention_seconds=14*86400):
        if min(max_bytes, max_object_bytes, max_objects) <= 0:
            raise ValueError('blob limits must be positive')
        if retention_seconds is not None and retention_seconds <= 0:
            raise ValueError('retention must be positive or None')
        self.store = store
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes, self.max_object_bytes = max_bytes, max_object_bytes
        self.max_objects, self.retention_seconds = max_objects, retention_seconds
        self._partial_lock = threading.Lock()
        with store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS blobs (
                  digest TEXT PRIMARY KEY, size INTEGER NOT NULL, state TEXT NOT NULL,
                  staging TEXT, created REAL NOT NULL, used REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blob_owners (
                  digest TEXT NOT NULL REFERENCES blobs(digest) ON DELETE CASCADE,
                  owner TEXT NOT NULL, PRIMARY KEY(digest,owner)
                );
            ''')
            db.execute(REFERENCE_DDL)

    @staticmethod
    def digest(value):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
            raise WorkloadError('invalid content digest')
        return value

    def put(self, owner, digest, size, source):
        name(owner, 'owner')
        digest = self.digest(digest)
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= self.max_object_bytes:
            raise WorkloadError('object size exceeds limit', 413)
        now = self.store.clock()
        staging = '.upload-'+uuid.uuid4().hex
        existing = False
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM blobs WHERE digest=?', (digest,)).fetchone()
            if row:
                if row['state'] != 'ready':
                    raise WorkloadError('object upload already in progress', 409)
                if row['size'] != size:
                    raise WorkloadError('digest size conflict', 409)
                existing = True
            else:
                count, used = db.execute('SELECT count(*),coalesce(sum(size),0) FROM blobs').fetchone()
                if count >= self.max_objects or used+size > self.max_bytes:
                    raise WorkloadError('content store capacity exhausted', 429)
                db.execute("INSERT INTO blobs VALUES(?,?,'uploading',?,?,?)", (digest, size, staging, now, now))
        try:
            hasher, left = hashlib.sha256(), size
            with (nullcontext(None) if existing else (self.root/staging).open('xb')) as out:
                while left:
                    part = source.read(min(1024*1024, left))
                    if not part:
                        raise WorkloadError('incomplete content upload')
                    if out is not None:
                        out.write(part)
                    hasher.update(part)
                    left -= len(part)
                if out is not None:
                    out.flush()
                    os.fsync(out.fileno())
            if hasher.hexdigest() != digest:
                raise WorkloadError('content digest mismatch', 409)
            with self.store.transaction() as db:
                # Existing bytes must be supplied and verified as well: knowing
                # another owner's digest must not confer read access to it.
                if not existing:
                    os.replace(self.root/staging, self.root/digest)
                    db.execute("UPDATE blobs SET state='ready',staging=NULL,used=? WHERE digest=?", (now,digest))
                db.execute('INSERT OR IGNORE INTO blob_owners VALUES(?,?)', (digest,owner))
            return {'digest': digest, 'size': size}
        except BaseException:
            if not existing:
                with self.store.transaction() as db:
                    db.execute("DELETE FROM blobs WHERE digest=? AND state='uploading'", (digest,))
            raise
        finally:
            (self.root/staging).unlink(missing_ok=True)

    # ---- resumable upload -------------------------------------------------
    # A partial upload is one private staging file per (owner, digest), grown only at
    # its current end, so a retry on ANY route resumes at the offset the authority
    # holds and no byte is ever stored twice. Bounded by count and idle age; a
    # restart discards partials (recover()) and the client simply resumes at 0.
    MAX_PARTIALS = 16
    PARTIAL_IDLE_SECONDS = 3600
    MAX_CHUNK_BYTES = 64*1024*1024

    def _partial_path(self, owner, digest):
        return self.root/('.partial-'+hashlib.sha256((owner+'\0'+digest).encode()).hexdigest()[:40])

    def _sweep_partials(self):
        cutoff = time.time()-self.PARTIAL_IDLE_SECONDS
        for path in self.root.glob('.partial-*'):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)

    def upload_offset(self, owner, digest):
        """Bytes already staged for this owner's upload of `digest` (0 when none)."""
        name(owner, 'owner')
        path = self._partial_path(owner, self.digest(digest))
        with self._partial_lock:
            return path.stat().st_size if path.is_file() else 0

    def completed_size(self, owner, digest):
        """Size of a verified object this owner already holds, else None."""
        with self.store.transaction() as db:
            row = db.execute("SELECT b.size FROM blobs b JOIN blob_owners o USING(digest) "
                             "WHERE b.digest=? AND o.owner=? AND b.state='ready'", (self.digest(digest), owner)).fetchone()
        return row['size'] if row else None

    def put_range(self, owner, digest, total, offset, length, source, chunk_digest=None):
        """Append bytes [offset, offset+length) of a `total`-byte object.

        The offset must equal what is staged; otherwise WorkloadError(409) carries
        `offset` so the caller resynchronises (a chunk whose acknowledgement was lost
        is the normal case). The final chunk verifies the digest and commits exactly
        as put() does. Returns {digest,size,offset,complete}."""
        name(owner, 'owner')
        digest = self.digest(digest)
        if (any(isinstance(v, bool) or not isinstance(v, int) for v in (total, offset, length)) or
                not 0 <= total <= self.max_object_bytes or length <= 0 or offset < 0 or offset+length > total):
            raise WorkloadError('invalid upload range', 400)
        if length > self.MAX_CHUNK_BYTES:
            raise WorkloadError('upload chunk exceeds bound', 413)
        path = self._partial_path(owner, digest)
        with self._partial_lock:
            have = path.stat().st_size if path.is_file() else 0
            if offset != have:
                error = WorkloadError('upload offset mismatch', 409)
                error.offset = have
                raise error
            if offset == 0:
                self._sweep_partials()
                with self.store.transaction() as db:
                    count, used = db.execute('SELECT count(*),coalesce(sum(size),0) FROM blobs').fetchone()
                partials = list(self.root.glob('.partial-*'))
                if (len(partials) >= self.MAX_PARTIALS or count >= self.max_objects or
                        used+total+sum(p.stat().st_size for p in partials) > self.max_bytes):
                    raise WorkloadError('content store capacity exhausted', 429)
            hasher_needed = offset+length == total
            try:
                chunk_hasher = hashlib.sha256()
                with path.open('ab') as out:
                    left = length
                    while left:
                        part = source.read(min(1024*1024, left))
                        if not part:
                            raise WorkloadError('incomplete content upload')
                        out.write(part)
                        chunk_hasher.update(part)
                        left -= len(part)
                    out.flush()
                    os.fsync(out.fileno())
                if chunk_digest is not None and chunk_hasher.hexdigest() != chunk_digest:
                    raise WorkloadError('chunk digest mismatch', 400)
            except BaseException:
                # A torn chunk must not leave a misaligned tail: truncate back.
                if path.exists():
                    with path.open('r+b') as out:
                        out.truncate(have)
                raise
            if not hasher_needed:
                return {'digest': digest, 'size': total, 'offset': offset+length, 'complete': False}
            hasher = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024*1024), b''):
                    hasher.update(block)
            if hasher.hexdigest() != digest:
                path.unlink(missing_ok=True)
                raise WorkloadError('content digest mismatch', 409)
            now = self.store.clock()
            with self.store.transaction() as db:
                row = db.execute('SELECT * FROM blobs WHERE digest=?', (digest,)).fetchone()
                if row and (row['state'] != 'ready' or row['size'] != total):
                    path.unlink(missing_ok=True)
                    raise WorkloadError('object upload already in progress' if row['state'] != 'ready'
                                        else 'digest size conflict', 409)
                if row:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(path, self.root/digest)
                    db.execute("INSERT INTO blobs VALUES(?,?,'ready',NULL,?,?)", (digest, total, now, now))
                db.execute('INSERT OR IGNORE INTO blob_owners VALUES(?,?)', (digest, owner))
            return {'digest': digest, 'size': total, 'offset': total, 'complete': True}

    @contextmanager
    def open(self, owner, digest):
        digest = self.digest(digest)
        with self.store.transaction() as db:
            row = db.execute("SELECT b.size FROM blobs b JOIN blob_owners o USING(digest) "
                             "WHERE b.digest=? AND o.owner=? AND b.state='ready'", (digest,owner)).fetchone()
            if not row:
                raise WorkloadError('content not found', 404)
            # Open before releasing the transaction: a concurrent prune cannot
            # replace the file between permission checking and obtaining a handle.
            stream = (self.root/digest).open('rb')
            db.execute('UPDATE blobs SET used=? WHERE digest=?', (self.store.clock(),digest))
        try:
            yield stream, row['size']
        finally:
            stream.close()

    @contextmanager
    def open_authority(self, digest):
        """Read a verified object for an authority-owned protocol purpose."""
        digest = self.digest(digest)
        with self.store.transaction() as db:
            row = db.execute("SELECT size FROM blobs WHERE digest=? AND state='ready'", (digest,)).fetchone()
            if not row:
                raise WorkloadError('content not found', 404)
            stream = (self.root/digest).open('rb')
            db.execute('UPDATE blobs SET used=? WHERE digest=?', (self.store.clock(), digest))
        try:
            yield stream, row['size']
        finally:
            stream.close()

    def recover(self):
        """Startup only, under the authority's singleton lock."""
        with self.store.transaction() as db:
            for row in db.execute("SELECT digest,staging FROM blobs WHERE state='uploading'").fetchall():
                if row['staging']:
                    (self.root/row['staging']).unlink(missing_ok=True)
                (self.root/row['digest']).unlink(missing_ok=True)
            db.execute("DELETE FROM blobs WHERE state='uploading'")
            known = {r[0] for r in db.execute('SELECT digest FROM blobs')}
            # A crash after rename and before commit can leave an unregistered
            # file. The directory is exclusively owned by this content store.
            for path in self.root.iterdir():
                if path.name not in known and path.is_file():
                    path.unlink()

    def prune(self):
        if self.retention_seconds is None:
            return
        now = self.store.clock()
        with self.store.transaction() as db:
            # Materialize the bounded reference set once, rather than walking
            # every attempt's JSON again for each object in the content store.
            rows = db.execute("WITH referenced(digest) AS MATERIALIZED ("
                "SELECT value FROM blob_references, json_each(blob_references.digests) UNION "
                "SELECT json_extract(spec,'$.input_digest') FROM jobs UNION "
                "SELECT json_extract(input.value,'$.digest') FROM jobs, "
                "json_each(jobs.spec,'$.input_objects') input UNION "
                "SELECT json_extract(artifact.value,'$.digest') FROM attempts, "
                "json_each(attempts.result,'$.result.artifacts') artifact UNION "
                "SELECT archive_digest FROM handler_releases) "
                "SELECT digest FROM blobs WHERE state='ready' AND used<? "
                "AND digest NOT IN (SELECT digest FROM referenced)",
                (now-self.retention_seconds,)).fetchall()
            for row in rows:
                (self.root/row['digest']).unlink(missing_ok=True)
                db.execute('DELETE FROM blobs WHERE digest=?', (row['digest'],))
