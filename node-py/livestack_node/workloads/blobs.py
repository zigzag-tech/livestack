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
                 max_objects=20000, retention_seconds=14*86400, guard=None, tiers=None, gc_batch=256):
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
        # Storage headroom (openspec/changes/storage-headroom-admission): `guard` is a
        # storage_bounds.HeadroomGuard, `tiers` retention_tiers.RetentionTiers; both None =
        # today's behaviour. Reload swaps them (replace_policy).
        self.guard, self.tiers, self.gc_batch = guard, tiers, gc_batch
        self._gc_lock, self._gc_last, self.gc_runs = threading.Lock(), None, 0
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
            # Reference age lives beside blob_references (not in it): older writers and tests insert
            # four positional columns. Rows without an age start their TTL clock when first seen.
            db.execute('CREATE TABLE IF NOT EXISTS blob_reference_ages (owner TEXT NOT NULL, name TEXT NOT NULL, '
                       'updated REAL NOT NULL, retain INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner,name))')
            self._age_references(db, store.clock())

    @staticmethod
    def digest(value):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
            raise WorkloadError('invalid content digest')
        return value

    def replace_policy(self, guard, tiers, gc_batch):
        self.guard, self.tiers, self.gc_batch = guard, tiers, gc_batch

    @property
    def effective_max_bytes(self):
        return self.max_bytes if self.guard is None else self.guard.snapshot()['effective_max_bytes']

    def _held(self, digest, size):
        with self.store.transaction() as db:
            return db.execute("SELECT 1 FROM blobs WHERE digest=? AND state='ready' AND size=?",
                              (digest, size)).fetchone() is not None

    def admit_headroom(self, size):
        """Refuse (HTTP 507, named) bytes that would leave the filesystem below its floor, after
        at most one bounded GC pass per refresh window. Never deletes a referenced object."""
        if self.guard is None:
            return
        deficit = self.guard.deficit(size)
        if deficit == 0:
            return
        if deficit is None:
            raise WorkloadError('storage_headroom_unknown: the objects filesystem cannot be read, so room for '
                                '%d bytes cannot be shown; refusing' % size, 507)
        gc = self._headroom_gc(deficit)
        snap = self.guard.snapshot()
        if self.guard.deficit(size, snap=snap) == 0:
            return
        detail = ''
        if not gc.get('candidates'):
            owners = ', '.join('%s %.1f GiB' % (o, b/2**30) for o, b in gc.get('largest_owners', []))
            detail = '; all remaining bytes referenced' + (' (largest owners: %s)' % owners if owners else '')
        raise WorkloadError('storage_headroom: objects filesystem has %.1f GiB free, floor %.1f GiB '
                            '(after GC freed %.1f GiB)%s'
                            % (snap['free_bytes']/2**30, snap['floor_bytes']/2**30, gc.get('freed', 0)/2**30, detail), 507)

    def _headroom_gc(self, deficit):
        with self._gc_lock:
            now = self.guard.clock()
            if self._gc_last and now-self._gc_last['at'] < self.guard.bounds.refresh_seconds:
                return self._gc_last
            result = self.collect(deficit)
            self._gc_last = dict(result, at=now)
            self.gc_runs += 1
            return self._gc_last

    GC_MIN_AGE_SECONDS = 3600  # a fresh upload is not yet referenced by the job that will use it

    def collect(self, deficit):
        """One bounded pass: expire references by rule, then delete UNREFERENCED ready objects
        oldest-use first until `deficit` bytes are freed or gc_batch objects are gone."""
        freed, deleted, candidates = 0, 0, 0
        with self.store.transaction() as db:
            self._expire_references(db, self.store.clock())
            rows = self._unreferenced(db, self.store.clock()-self.GC_MIN_AGE_SECONDS, 'ORDER BY used LIMIT ?',
                                      (self.gc_batch,))
            candidates = len(rows)
            for row in rows:
                if freed >= deficit:
                    break
                (self.root/row['digest']).unlink(missing_ok=True)
                db.execute('DELETE FROM blobs WHERE digest=?', (row['digest'],))
                freed += row['size']
                deleted += 1
            largest = [] if freed else [(r[0], r[1]) for r in db.execute(
                'SELECT o.owner,sum(b.size) s FROM blob_owners o JOIN blobs b USING(digest) '
                "WHERE b.state='ready' GROUP BY o.owner ORDER BY s DESC LIMIT 5")]
        return dict(freed=freed, deleted=deleted, candidates=candidates, largest_owners=largest)

    REFERENCED = ("WITH referenced(digest) AS MATERIALIZED ("
                  "SELECT value FROM blob_references, json_each(blob_references.digests) UNION "
                  "SELECT json_extract(spec,'$.input_digest') FROM jobs UNION "
                  "SELECT json_extract(input.value,'$.digest') FROM jobs, "
                  "json_each(jobs.spec,'$.input_objects') input UNION "
                  "SELECT json_extract(artifact.value,'$.digest') FROM attempts, "
                  "json_each(attempts.result,'$.result.artifacts') artifact UNION "
                  "SELECT archive_digest FROM handler_releases) ")

    def _unreferenced(self, db, cutoff, tail='', args=()):
        return db.execute(self.REFERENCED+"SELECT digest,size FROM blobs WHERE state='ready' AND used<? "
                          "AND digest NOT IN (SELECT digest FROM referenced) "+tail, (cutoff,)+tuple(args)).fetchall()

    @staticmethod
    def _age_references(db, now):
        db.execute('INSERT OR IGNORE INTO blob_reference_ages SELECT owner,name,?,0 FROM blob_references', (now,))
        db.execute('DELETE FROM blob_reference_ages WHERE NOT EXISTS (SELECT 1 FROM blob_references r '
                   'WHERE r.owner=blob_reference_ages.owner AND r.name=blob_reference_ages.name)')

    def _reference_rows(self, db):
        """Every reference with its byte weight and the rule that governs it (None = unbounded)."""
        self._age_references(db, self.store.clock())
        sizes = {(r[0], r[1]): r[2] or 0 for r in db.execute(
            'SELECT r.owner,r.name,sum(b.size) FROM blob_references r, json_each(r.digests) j '
            'JOIN blobs b ON b.digest=j.value GROUP BY r.owner,r.name')}
        rows = []
        for r in db.execute('SELECT owner,name,updated,retain FROM blob_reference_ages'):
            rule = self.tiers.rule_for(r['owner'], r['name']) if self.tiers else None
            rows.append(dict(owner=r['owner'], name=r['name'], updated=r['updated'] or 0, retain=bool(r['retain']),
                             bytes=sizes.get((r['owner'], r['name']), 0), rule=rule))
        return rows

    def _expirable(self, rows, now):
        """References beyond a rule's newest `keep_newest` AND older than its ttl (and not retained)."""
        groups = {}
        for row in rows:
            if row['rule'] is not None:
                groups.setdefault(row['rule'], []).append(row)
        out = []
        for rule, members in groups.items():
            members.sort(key=lambda m: (-m['updated'], m['name']))
            for row in members[rule.keep_newest:]:
                if rule.ttl_seconds is not None and not row['retain'] and now-row['updated'] > rule.ttl_seconds:
                    out.append(row)
        return out

    def _expire_references(self, db, now):
        if self.tiers is None or not self.tiers.references:
            return []
        doomed = self._expirable(self._reference_rows(db), now)
        for row in doomed:
            db.execute('DELETE FROM blob_references WHERE owner=? AND name=?', (row['owner'], row['name']))
            db.execute('DELETE FROM blob_reference_ages WHERE owner=? AND name=?', (row['owner'], row['name']))
        return doomed

    def reference_report(self, db=None, now=None):
        now = self.store.clock() if now is None else now
        def build(db):
            rows = self._reference_rows(db)
            unbounded = {}
            for row in rows:
                if row['rule'] is None:
                    entry = unbounded.setdefault(row['owner'], dict(owner=row['owner'], count=0, bytes=0))
                    entry['count'] += 1
                    entry['bytes'] += row['bytes']
            doomed = self._expirable(rows, now)
            ranked = sorted(unbounded.values(), key=lambda e: -e['bytes'])
            return dict(
                unbounded_references=dict(count=sum(e['count'] for e in ranked), bytes=sum(e['bytes'] for e in ranked),
                                          owners=ranked[:20]),
                retained_bytes=sum(r['bytes'] for r in rows if r['retain']),
                would_expire=dict(count=len(doomed), bytes=sum(r['bytes'] for r in doomed),
                                  items=[dict(owner=r['owner'], name=r['name']) for r in doomed[:20]]))
        if db is not None:
            return build(db)
        with self.store.transaction() as conn:
            return build(conn)

    def retention_plan(self):
        """Dry run: what a retention pass would delete now. Deletes nothing."""
        now = self.store.clock()
        with self.store.transaction() as db:
            report = self.reference_report(db, now)
            cutoff = now-(self.retention_seconds or 0)
            rows = self._unreferenced(db, cutoff) if self.retention_seconds is not None else []
        return dict(references=report, unreferenced_blobs=dict(
            count=len(rows), bytes=sum(r['size'] for r in rows), older_than_seconds=self.retention_seconds))

    def status(self):
        with self.store.transaction() as db:
            count, used = db.execute("SELECT count(*),coalesce(sum(size),0) FROM blobs WHERE state='ready'").fetchone()
        out = dict(objects=count, used_bytes=used, max_bytes=self.max_bytes, headroom_gc_runs=self.gc_runs,
                   last_gc={k: v for k, v in (self._gc_last or {}).items() if k != 'at'} or None)
        if self.guard is not None:
            out['bound'] = self.guard.snapshot()
            out['events'] = list(self.guard.events)
        out.update(self.reference_report())
        return out

    def put(self, owner, digest, size, source):
        name(owner, 'owner')
        digest = self.digest(digest)
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= self.max_object_bytes:
            raise WorkloadError('object size exceeds limit', 413)
        now = self.store.clock()
        staging = '.upload-'+uuid.uuid4().hex
        existing = False
        if not self._held(digest, size):
            self.admit_headroom(size)
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
                if count >= self.max_objects or used+size > self.effective_max_bytes:
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
                if not self._held(digest, total):
                    self.admit_headroom(total)
                with self.store.transaction() as db:
                    count, used = db.execute('SELECT count(*),coalesce(sum(size),0) FROM blobs').fetchone()
                partials = list(self.root.glob('.partial-*'))
                if (len(partials) >= self.MAX_PARTIALS or count >= self.max_objects or
                        used+total+sum(p.stat().st_size for p in partials) > self.effective_max_bytes):
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
        now = self.store.clock()
        with self.store.transaction() as db:
            self._expire_references(db, now)
        if self.retention_seconds is None:
            return
        with self.store.transaction() as db:
            # Materialize the bounded reference set once, rather than walking
            # every attempt's JSON again for each object in the content store.
            for row in self._unreferenced(db, now-self.retention_seconds):
                (self.root/row['digest']).unlink(missing_ok=True)
                db.execute('DELETE FROM blobs WHERE digest=?', (row['digest'],))
