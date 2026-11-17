"""Explicit, expiring read handoffs; grants never confer write or re-share rights."""
import re

from .model import WorkloadError, name

MAX_JOBS, MAX_OBJECTS, MAX_DIGESTS = 4096, 16384, 128


def _recipient(recipient, ttl):
    name(recipient, 'recipient')
    if type(ttl) is not int or not 60 <= ttl <= 86400:
        raise WorkloadError('read handoff TTL must be 60..86400 seconds')


def _objects(db, owner, recipient, digests, now, expires):
    import json
    if (not isinstance(digests, list) or len(digests) > MAX_DIGESTS or
            any(not isinstance(d, str) or not re.fullmatch('[0-9a-f]{64}', d) for d in digests) or
            len(set(digests)) != len(digests)):
        raise WorkloadError('read handoff requires at most 128 unique digests')
    encoded = json.dumps(digests)
    count = db.execute("SELECT count(*) FROM blob_owners o JOIN blobs b USING(digest) "
        "WHERE o.owner=? AND b.state='ready' AND o.digest IN (SELECT value FROM json_each(?))",
        (owner, encoded)).fetchone()[0]
    if count != len(digests):
        raise WorkloadError('read handoff content not owned', 404)
    db.execute('DELETE FROM shared_objects WHERE expires<=?', (now,))
    additions = db.execute('SELECT count(*) FROM json_each(?) d WHERE NOT EXISTS '
        '(SELECT 1 FROM shared_objects s WHERE s.owner=? AND s.recipient=? AND s.digest=d.value)',
        (encoded, owner, recipient)).fetchone()[0]
    if db.execute('SELECT count(*) FROM shared_objects').fetchone()[0]+additions > MAX_OBJECTS:
        raise WorkloadError('read handoff object capacity exhausted', 429)
    db.execute('INSERT INTO shared_objects(owner,recipient,digest,expires) '
        'SELECT ?,?,value,? FROM json_each(?) WHERE 1 '
        'ON CONFLICT(owner,recipient,digest) DO UPDATE SET expires=excluded.expires',
        (owner, recipient, expires, encoded))


def grant_objects(store, owner, recipient, digests, ttl=3600):
    _recipient(recipient, ttl)
    now = store.clock()
    with store.transaction() as db:
        _objects(db, owner, recipient, digests, now, now+ttl)
    return dict(recipient=recipient, digests=digests, expires=now+ttl)


def grant_job(store, owner, job_id, recipient, ttl=3600):
    _recipient(recipient, ttl)
    now = store.clock()
    with store.transaction() as db:
        job = store._job(db, job_id, owner)
        if job['state'] != 'succeeded':
            raise WorkloadError('read handoff job is not succeeded', 409)
        db.execute('DELETE FROM shared_jobs WHERE expires<=?', (now,))
        exists = db.execute('SELECT 1 FROM shared_jobs WHERE job=? AND recipient=?',
                            (job_id, recipient)).fetchone()
        if not exists and db.execute('SELECT count(*) FROM shared_jobs').fetchone()[0] >= MAX_JOBS:
            raise WorkloadError('read handoff job capacity exhausted', 429)
        artifacts = ((job.get('result') or {}).get('result') or {}).get('artifacts', [])
        digests = sorted({a['digest'] for a in artifacts if not a.get('local')})
        _objects(db, owner, recipient, digests, now, now+ttl)
        db.execute('INSERT INTO shared_jobs VALUES(?,?,?) ON CONFLICT(job,recipient) '
                   'DO UPDATE SET expires=excluded.expires', (job_id, recipient, now+ttl))
    return dict(job_id=job_id, recipient=recipient, expires=now+ttl)


def read_job(store, db, reader, job_id):
    job = store._job(db, job_id)
    if job['owner'] != reader and not db.execute(
            'SELECT 1 FROM shared_jobs WHERE job=? AND recipient=? AND expires>?',
            (job_id, reader, store.clock())).fetchone():
        raise WorkloadError('job not found', 404)
    return job
