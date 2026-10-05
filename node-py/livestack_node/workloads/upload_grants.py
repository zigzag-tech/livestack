"""One-object, owner-scoped, expiring upload grants.

A configured caller delegates ONE exact immutable object upload (digest + size)
into its own CAS namespace to a holder of an opaque bearer capability. The
capability authorizes only that PUT: no reads, jobs, or further grants. Only its
SHA-256 verifier is stored. openspec: issue-scoped-workload-upload-grants.
"""
import hashlib
import hmac
import re
import secrets
import threading

from .model import WorkloadError, name

MAX_GRANTS = 4096
MAX_UNEXPIRED_PER_OWNER = 512
MAX_EVENTS = 8192
TERMINAL_RETENTION_SECONDS = 24*3600
DEFAULT_EXPIRY_SECONDS = 900
MAX_EXPIRY_SECONDS = 3600
REQUEST_ID = re.compile(r'[A-Za-z0-9_.:@-]{1,128}')
GRANT_ID = re.compile(r'[0-9a-f]{32}')
FIELDS = {'request_id', 'digest', 'size', 'expires_in_seconds'}

DDL = '''
CREATE TABLE IF NOT EXISTS upload_grants (
  grant_id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL,
  digest TEXT NOT NULL, size INTEGER NOT NULL, verifier TEXT NOT NULL,
  previous_verifier TEXT, created REAL NOT NULL, expires REAL NOT NULL,
  state TEXT NOT NULL, finished REAL, UNIQUE(owner, request_id)
);
CREATE TABLE IF NOT EXISTS upload_grant_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, owner TEXT NOT NULL,
  request_id TEXT NOT NULL, digest TEXT, size INTEGER, outcome TEXT NOT NULL
);
'''


def _verifier(capability):
    return hashlib.sha256(capability.encode()).hexdigest()


class UploadGrants:
    def __init__(self, blobs):
        self.blobs, self.store = blobs, blobs.store
        self._active, self._lock = set(), threading.Lock()
        with self.store.transaction() as db:
            db.executescript(DDL)

    def _event(self, db, owner, request_id, digest, size, outcome):
        db.execute('INSERT INTO upload_grant_events(at,owner,request_id,digest,size,outcome) VALUES(?,?,?,?,?,?)',
                   (self.store.clock(), owner, request_id, digest, size, outcome))
        db.execute('DELETE FROM upload_grant_events WHERE id <= '
                   '(SELECT max(id) FROM upload_grant_events)-?', (MAX_EVENTS,))

    def _refuse(self, owner, request_id, digest, size, outcome, message, status):
        """Record the refusal durably, then raise (the caller's txn has rolled back)."""
        with self.store.transaction() as db:
            self._event(db, owner, request_id, digest, size, outcome)
        raise WorkloadError(message, status)

    def _collect(self, db, now):
        db.execute("UPDATE upload_grants SET state='expired',finished=? WHERE state='issued' AND expires<=?",
                   (now, now))
        db.execute('DELETE FROM upload_grants WHERE finished IS NOT NULL AND finished<?',
                   (now-TERMINAL_RETENTION_SECONDS,))

    def sweep(self):
        with self.store.transaction() as db:
            self._collect(db, self.store.clock())

    @staticmethod
    def _owned(db, owner, digest, size):
        return db.execute("SELECT 1 FROM blobs b JOIN blob_owners o USING(digest) WHERE b.digest=? "
                          "AND o.owner=? AND b.size=? AND b.state='ready'", (digest, owner, size)).fetchone()

    def issue(self, principal, body, base_url):
        if 'owner' in body:
            raise WorkloadError('upload grants target the caller\'s own object namespace only', 403)
        if not set(body) <= FIELDS or not {'request_id', 'digest', 'size'} <= set(body):
            raise WorkloadError('invalid upload grant fields')
        owner, request_id = principal.id, body['request_id']
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise WorkloadError('invalid request id')
        digest, size = self.blobs.digest(body['digest']), body['size']
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= self.blobs.max_object_bytes:
            raise WorkloadError('object size exceeds limit', 413)
        ttl = body.get('expires_in_seconds', DEFAULT_EXPIRY_SECONDS)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= MAX_EXPIRY_SECONDS:
            raise WorkloadError('expiry outside the allowed window')
        refusal = None
        with self.store.transaction() as db:
            now = self.store.clock()
            self._collect(db, now)
            row = db.execute('SELECT * FROM upload_grants WHERE owner=? AND request_id=?',
                             (owner, request_id)).fetchone()
            if row and (row['digest'], row['size']) != (digest, size):
                refusal = ('refused_binding_changed', 'upload grant binding changed for this request id', 409)
            elif row and (row['state'] == 'uploaded' or self._owned(db, owner, digest, size)):
                self._mark_uploaded(db, row['grant_id'], owner, request_id, digest, size, now, record=row['state'] != 'uploaded')
                return self._status_row(db.execute('SELECT * FROM upload_grants WHERE grant_id=?',
                                                   (row['grant_id'],)).fetchone())
            elif row and row['grant_id'] in self._active:
                refusal = ('refused_in_use', 'grant_in_use', 409)
            elif not row and (db.execute('SELECT count(*) FROM upload_grants').fetchone()[0] >= MAX_GRANTS
                              or db.execute("SELECT count(*) FROM upload_grants WHERE owner=? AND state='issued'",
                                            (owner,)).fetchone()[0] >= MAX_UNEXPIRED_PER_OWNER):
                refusal = ('refused_capacity', 'upload_grant_capacity', 429)
            else:
                capability = secrets.token_urlsafe(32)
                if row:
                    grant_id, outcome = row['grant_id'], 'rotated'
                    db.execute("UPDATE upload_grants SET previous_verifier=verifier,verifier=?,expires=?,"
                               "state='issued',finished=NULL WHERE grant_id=?",
                               (_verifier(capability), now+ttl, grant_id))
                else:
                    grant_id, outcome = secrets.token_hex(16), 'issued'
                    db.execute('INSERT INTO upload_grants VALUES(?,?,?,?,?,?,NULL,?,?,?,NULL)',
                               (grant_id, owner, request_id, digest, size, _verifier(capability),
                                now, now+ttl, 'issued'))
                self._event(db, owner, request_id, digest, size, outcome)
                return dict(grant_id=grant_id, request_id=request_id, digest=digest, size=size,
                            state='issued', expires_at=now+ttl, capability=capability,
                            upload_url=f'{base_url}/v1/workloads/upload-grants/{grant_id}/objects/{digest}')
        self._refuse(owner, request_id, digest, size, *refusal)

    def _mark_uploaded(self, db, grant_id, owner, request_id, digest, size, now, record=True):
        if record:
            db.execute("UPDATE upload_grants SET state='uploaded',finished=? WHERE grant_id=?", (now, grant_id))
            self._event(db, owner, request_id, digest, size, 'uploaded')

    @staticmethod
    def _status_row(row):
        out = dict(grant_id=row['grant_id'], request_id=row['request_id'], digest=row['digest'],
                   size=row['size'], state=row['state'], expires_at=row['expires'])
        if row['state'] == 'uploaded':
            out['uploaded_at'] = row['finished']
        return out

    def status(self, principal, request_id):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise WorkloadError('invalid request id')
        with self.store.transaction() as db:
            now = self.store.clock()
            self._collect(db, now)
            row = db.execute('SELECT * FROM upload_grants WHERE owner=? AND request_id=?',
                             (principal.id, request_id)).fetchone()
            if not row:
                raise WorkloadError('upload grant not found', 404)
            if row['state'] != 'uploaded' and self._owned(db, row['owner'], row['digest'], row['size']):
                # Crash/lost reply after the CAS commit: complete the receipt from
                # the durable owner/digest/size binding, never from the bytes.
                self._mark_uploaded(db, row['grant_id'], row['owner'], request_id, row['digest'], row['size'], now)
                row = db.execute('SELECT * FROM upload_grants WHERE grant_id=?', (row['grant_id'],)).fetchone()
            return self._status_row(row)

    def upload(self, handler, grant_id, digest):
        """Authorize with the bearer capability alone and stream into the CAS."""
        header = handler.headers.get('Authorization', '')
        presented = _verifier(header[7:]) if header.startswith('Bearer ') and len(header) > 7 else None
        if not GRANT_ID.fullmatch(grant_id) or presented is None:
            raise WorkloadError('upload capability required', 401)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM upload_grants WHERE grant_id=?', (grant_id,)).fetchone()
        if not row:
            raise WorkloadError('upload capability required', 401)
        owner, request_id = row['owner'], row['request_id']
        if not hmac.compare_digest(presented, row['verifier']):
            if row['previous_verifier'] and hmac.compare_digest(presented, row['previous_verifier']):
                self._refuse(owner, request_id, row['digest'], row['size'], 'refused_revoked',
                             'upload_grant_revoked', 403)
            raise WorkloadError('upload capability required', 401)
        if digest != row['digest']:
            self._refuse(owner, request_id, row['digest'], row['size'], 'refused_wrong_object',
                         'capability does not authorize this object', 403)
        if row['state'] == 'uploaded':
            return dict(grant_id=grant_id, digest=row['digest'], size=row['size'])
        if self.store.clock() >= row['expires']:
            with self.store.transaction() as db:
                db.execute("UPDATE upload_grants SET state='expired',finished=? WHERE grant_id=? AND state='issued'",
                           (self.store.clock(), grant_id))
            self._refuse(owner, request_id, row['digest'], row['size'], 'refused_expired',
                         'upload_grant_expired', 410)
        if handler.headers.get('Transfer-Encoding'):
            raise WorkloadError('transfer encoding is not supported')
        try:
            length = int(handler.headers.get('Content-Length', '-1'))
        except ValueError:
            length = -1
        if length != row['size']:
            self._refuse(owner, request_id, row['digest'], row['size'], 'refused_wrong_size',
                         'content length must equal the granted size', 403)
        with self._lock:
            if grant_id in self._active:
                raise WorkloadError('grant_in_use', 409)
            self._active.add(grant_id)
        try:
            from .object_routes import OBJECT_UPLOAD_IDLE_TIMEOUT_SECONDS
            previous = handler.connection.gettimeout()
            handler.connection.settimeout(OBJECT_UPLOAD_IDLE_TIMEOUT_SECONDS)
            try:
                self.blobs.put(owner, row['digest'], row['size'], handler.rfile)
            finally:
                handler.connection.settimeout(previous)
            handler._body_consumed = True
            with self.store.transaction() as db:
                self._mark_uploaded(db, grant_id, owner, request_id, row['digest'], row['size'], self.store.clock())
        finally:
            with self._lock:
                self._active.discard(grant_id)
        return dict(grant_id=grant_id, digest=row['digest'], size=row['size'])


def route_upload_grant(handler, method, parts):
    """Capability routes that must run BEFORE principal authentication.

    Returns None when `parts` is not the capability PUT route."""
    if len(parts) == 4 and parts[0] == 'upload-grants' and parts[2] == 'objects':
        if method != 'PUT':
            raise WorkloadError('unsupported upload grant operation', 405)
        return handler.server.upload_grants.upload(handler, parts[1], parts[3])
    return None


def route_upload_grant_owner(handler, principal, method, parts):
    """Owner routes: mint and status. Returns (handled, result)."""
    if parts[:1] != ['upload-grants'] or len(parts) not in (1, 2):
        return False, None
    if principal.role not in ('caller', 'admin') or not getattr(principal, 'upload_grants', False):
        raise WorkloadError('upload grants are not enabled for this principal', 403)
    grants = handler.server.upload_grants
    if parts == ['upload-grants'] and method == 'POST':
        host = handler.headers.get('Host') or '%s:%s' % handler.server.server_address[:2]
        base = getattr(handler.server, 'public_base_url', None) or f'http://{host}'
        return True, grants.issue(principal, handler.body(), base)
    if len(parts) == 2 and method == 'GET':
        return True, grants.status(principal, parts[1])
    raise WorkloadError('unsupported upload grant operation', 405)
