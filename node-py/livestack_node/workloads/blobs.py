"""Bounded immutable content storage with principal-scoped access.

One digest names one byte sequence. Objects referenced by durable jobs cannot be
pruned. The authority serializes quota reservation; uploads stream to private
staging files rather than buffering whole source archives in memory.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import os
from pathlib import Path
import re
import time
import uuid

from .model import WorkloadError, name


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
            rows = db.execute("SELECT digest FROM blobs WHERE state='ready' AND used<? "
                "AND NOT EXISTS(SELECT 1 FROM jobs WHERE json_extract(spec,'$.input_digest')=blobs.digest)",
                (now-self.retention_seconds,)).fetchall()
            for row in rows:
                (self.root/row['digest']).unlink(missing_ok=True)
                db.execute('DELETE FROM blobs WHERE digest=?', (row['digest'],))
