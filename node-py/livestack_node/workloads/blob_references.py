"""Bounded, revision-fenced source roots independent of executing jobs."""
import json

from .model import WorkloadError, name

REFERENCE_DDL = '''CREATE TABLE IF NOT EXISTS blob_references (
  owner TEXT NOT NULL, name TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision > 0 AND revision <= 9007199254740991),
  digests TEXT NOT NULL CHECK(json_valid(digests) AND length(digests) <= 2048),
  PRIMARY KEY(owner,name)
)'''
MAX_REFERENCES = 1024
MAX_DIGESTS = 16
MAX_REVISION = 9007199254740991


class BlobReferences:
    def __init__(self, blobs):
        self.blobs = blobs

    @staticmethod
    def _value(row):
        return dict(revision=row['revision'], digests=json.loads(row['digests'])) if row else dict(revision=0, digests=[])

    def get(self, owner, key):
        name(owner, 'owner')
        name(key, 'reference')
        with self.blobs.store.transaction() as db:
            return self._value(db.execute('SELECT * FROM blob_references WHERE owner=? AND name=?', (owner,key)).fetchone())

    def replace(self, owner, key, digests, expected_revision):
        name(owner, 'owner')
        name(key, 'reference')
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or not 0 <= expected_revision < MAX_REVISION):
            raise WorkloadError('invalid reference revision')
        if not isinstance(digests, list) or len(digests) > MAX_DIGESTS:
            raise WorkloadError('reference digest limit exceeded')
        digests = sorted(set(self.blobs.digest(value) for value in digests))
        with self.blobs.store.transaction() as db:
            row = db.execute('SELECT * FROM blob_references WHERE owner=? AND name=?', (owner,key)).fetchone()
            current = self._value(row)
            if row and current['digests'] == digests:
                return current
            if current['revision'] != expected_revision:
                raise WorkloadError('reference revision conflict', 409)
            if not row and db.execute('SELECT count(*) FROM blob_references').fetchone()[0] >= MAX_REFERENCES:
                raise WorkloadError('reference capacity exhausted', 429)
            for digest in digests:
                ready = db.execute("SELECT 1 FROM blobs b JOIN blob_owners o USING(digest) "
                    "WHERE b.digest=? AND o.owner=? AND b.state='ready'", (digest,owner)).fetchone()
                if not ready:
                    raise WorkloadError('referenced content not found', 404)
            revision = expected_revision+1
            db.execute('INSERT INTO blob_references VALUES(?,?,?,?) ON CONFLICT(owner,name) '
                       'DO UPDATE SET revision=excluded.revision,digests=excluded.digests',
                       (owner,key,revision,json.dumps(digests,separators=(',',':'))))
            # Empty references retain their revision: deleting a row would let
            # stale pre-deletion writers pass after a name was recreated.
            return dict(revision=revision, digests=digests)


def route_reference(handler, principal, method, parts):
    if len(parts) != 2 or parts[0] != 'references':
        return False
    if principal.role not in ('caller', 'admin'):
        raise WorkloadError('reference operation requires caller authority', 403)
    refs = BlobReferences(handler.server.blobs)
    if method == 'GET':
        result = refs.get(principal.id, parts[1])
    elif method == 'POST':
        body = handler.body()
        if set(body) != {'digests', 'expected_revision'}:
            raise WorkloadError('invalid reference fields')
        result = refs.replace(principal.id, parts[1], body['digests'], body['expected_revision'])
    else:
        raise WorkloadError('unsupported reference operation', 405)
    handler.respond(200, result)
    return True
