"""Worker claims: who stopped a worker taking work, until when, and a version to compare-and-swap.

Replaces the shared-file bit `claim_enabled` in authority.json as the authority's source of truth
(openspec/changes/declarative-worker-rollout, D2). The failures it removes: a drain with no owner
and no end (forgotten after an interrupted roll), and two writers each reading the whole file,
editing one principal and writing it back (the second erased the first).

Semantics:
- A row per worker: `enabled`, `generation` (+1 on every change), `owner`, `reason`, `expires_at`.
- A drain through the API MUST carry an expiry (`ttl_seconds` or `until`), at most MAX_TTL_SECONDS.
- An expired drain re-enables the worker, lazily (every read treats it as enabled) and on the
  authority's 30 s tick (`expire`, which also leaves the ledger record `drain_expired`).
  Exception: a row marked `needs_operator` (a failed rollback) does NOT expire into service.
- Writers pass `if_generation`; a stale one is refused `claim_generation_conflict`.
- A drain held by another owner is refused `drain_held_by:<owner>` unless an admin passes `force`.
- authority.json `claim_enabled` still works during migration (`sync_file`): the value is imported
  when a worker has no row, and a later change of that value is applied as a claim change by owner
  `file:authority.json` with no expiry (the legacy behaviour, logged). An unchanged file value never
  overrides the row, so an API drain survives every SIGHUP.
All reads used by placement/assignment/capacity are one set-based query (`draining`).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from .model import WorkloadError, name

MAX_TTL_SECONDS = 24 * 3600
FILE_OWNER = 'file:authority.json'
IMPORT_OWNER = 'import:authority.json'
MAX_REASON = 240


def _row(row):
    return None if row is None else dict(
        worker=row['worker'], enabled=bool(row['enabled']), generation=row['generation'],
        owner=row['owner'], reason=row['reason'], expires_at=row['expires_at'],
        needs_operator=bool(row['needs_operator']), updated_at=row['updated_at'])


def _live_drain(row, now):
    """True while the row still withholds claims."""
    if row is None or row['enabled']:
        return False
    return bool(row['needs_operator']) or row['expires_at'] is None or row['expires_at'] > now


class ActionLedger:
    """Append-only JSON lines, bounded by size x files (the writer is the enforcer).

    Never raises: a ledger failure is logged and counted, the claim change it describes already
    happened and must not be undone by a disk problem."""

    def __init__(self, path, *, max_bytes=8 * 1024 * 1024, max_files=3, clock=time.time):
        self.path, self.max_bytes, self.max_files, self.clock = str(path), max_bytes, max_files, clock
        self.failures = 0
        self._lock = threading.Lock()

    def append(self, kind, **fields):
        record = dict(at=round(self.clock(), 3), kind=kind, **fields)
        line = json.dumps(record, separators=(',', ':'), default=str)[:16384] + '\n'
        with self._lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) + len(line) > self.max_bytes:
                    for index in range(self.max_files - 1, 0, -1):
                        older = f'{self.path}.{index}'
                        newer = self.path if index == 1 else f'{self.path}.{index - 1}'
                        if os.path.exists(newer):
                            os.replace(newer, older)
                with open(self.path, 'a') as handle:
                    handle.write(line)
            except OSError as error:
                self.failures += 1
                logging.error('action_ledger_write_failed: %s', error)
        return record


class Claims:
    """Claim operations over a WorkloadStore's database."""

    def __init__(self, store, ledger=None, *, max_ttl=MAX_TTL_SECONDS):
        self.store, self.ledger, self.max_ttl = store, ledger, max_ttl

    # -- reads ---------------------------------------------------------------------------------

    def get(self, worker):
        with self.store.transaction() as db:
            return _row(db.execute('SELECT * FROM worker_claims WHERE worker=?', (worker,)).fetchone())

    def listing(self):
        now = self.store.clock()
        with self.store.transaction() as db:
            rows = db.execute('SELECT * FROM worker_claims ORDER BY worker LIMIT ?',
                              (self.store.limits.workers * 2,)).fetchall()
        return dict(now=round(now, 3), max_ttl_seconds=self.max_ttl, claims=[
            dict(_row(r), draining=_live_drain(r, now)) for r in rows])

    # -- writes --------------------------------------------------------------------------------

    def _record(self, kind, **fields):
        if self.ledger is not None:
            self.ledger.append(kind, **fields)

    @staticmethod
    def _generation_check(row, if_generation):
        if if_generation is None:
            return
        if type(if_generation) is not int or if_generation < 0:
            raise WorkloadError('invalid if_generation')
        have = 0 if row is None else row['generation']
        if have != if_generation:
            raise WorkloadError(f'claim_generation_conflict: have {have}, caller expected {if_generation}', 409)

    def _expiry(self, now, ttl_seconds, until):
        if ttl_seconds is None and until is None:
            raise WorkloadError('drain_requires_expiry: pass ttl_seconds or until', 400)
        if ttl_seconds is not None and until is not None:
            raise WorkloadError('pass ttl_seconds or until, not both', 400)
        if ttl_seconds is not None:
            if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or ttl_seconds <= 0:
                raise WorkloadError('ttl_seconds must be a positive number', 400)
            expires = now + ttl_seconds
        else:
            if isinstance(until, bool) or not isinstance(until, (int, float)) or until <= now:
                raise WorkloadError('until must be a future epoch time', 400)
            expires = float(until)
        if expires - now > self.max_ttl:
            raise WorkloadError(f'drain_ttl_exceeds_cap: at most {int(self.max_ttl)} s; use hold in the '
                                'rollout spec for an intentional long hold', 400)
        return expires

    def drain(self, worker, *, owner, reason='', ttl_seconds=None, until=None, if_generation=None,
              force=False, needs_operator=False, known=None):
        name(worker, 'worker'); name(owner, 'owner')
        if not isinstance(reason, str) or len(reason) > MAX_REASON:
            raise WorkloadError('invalid reason')
        if known is not None and worker not in known:
            raise WorkloadError('unknown_worker', 404)
        now = self.store.clock()
        expires = self._expiry(now, ttl_seconds, until)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM worker_claims WHERE worker=?', (worker,)).fetchone()
            self._generation_check(row, if_generation)
            if _live_drain(row, now) and row['owner'] != owner and not force:
                raise WorkloadError(f"drain_held_by:{row['owner']}", 409)
            generation = 1 if row is None else row['generation'] + 1
            db.execute('INSERT INTO worker_claims(worker,enabled,generation,owner,reason,expires_at,'
                       'needs_operator,file_value,updated_at) VALUES(?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(worker) DO UPDATE SET enabled=0,generation=excluded.generation,'
                       'owner=excluded.owner,reason=excluded.reason,expires_at=excluded.expires_at,'
                       'needs_operator=excluded.needs_operator,updated_at=excluded.updated_at',
                       (worker, 0, generation, owner, reason, expires, int(bool(needs_operator)),
                        None if row is None else row['file_value'], now))
            new = _row(db.execute('SELECT * FROM worker_claims WHERE worker=?', (worker,)).fetchone())
        self._record('drain', worker=worker, owner=owner, reason=reason, generation=generation,
                     expires_at=expires, forced=bool(force and row is not None and _live_drain(row, now)
                                                     and row['owner'] != owner),
                     needs_operator=bool(needs_operator))
        return new

    def enable(self, worker, *, owner, reason='', if_generation=None, force=False, operator=False, known=None):
        """`operator` (admin) may clear a needs_operator hold; others get `needs_operator`."""
        name(worker, 'worker'); name(owner, 'owner')
        if known is not None and worker not in known:
            raise WorkloadError('unknown_worker', 404)
        now = self.store.clock()
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM worker_claims WHERE worker=?', (worker,)).fetchone()
            self._generation_check(row, if_generation)
            if row is not None and row['needs_operator'] and not row['enabled'] and not operator:
                raise WorkloadError('needs_operator: only an operator may enable this worker', 403)
            clearing_hold = operator and row is not None and row['needs_operator']
            if _live_drain(row, now) and row['owner'] != owner and not force and not clearing_hold:
                raise WorkloadError(f"drain_held_by:{row['owner']}", 409)
            generation = 1 if row is None else row['generation'] + 1
            db.execute('INSERT INTO worker_claims(worker,enabled,generation,owner,reason,expires_at,'
                       'needs_operator,file_value,updated_at) VALUES(?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(worker) DO UPDATE SET enabled=1,generation=excluded.generation,'
                       'owner=excluded.owner,reason=excluded.reason,expires_at=NULL,needs_operator=0,'
                       'updated_at=excluded.updated_at',
                       (worker, 1, generation, owner, reason, None, 0,
                        None if row is None else row['file_value'], now))
            new = _row(db.execute('SELECT * FROM worker_claims WHERE worker=?', (worker,)).fetchone())
        self._record('enable', worker=worker, owner=owner, reason=reason, generation=generation)
        return new

    def expire(self):
        """The 30 s tick: re-enable workers whose drain ran out. Returns the workers re-enabled."""
        now = self.store.clock()
        expired = []
        with self.store.transaction() as db:
            for row in db.execute('SELECT * FROM worker_claims WHERE enabled=0 AND needs_operator=0 '
                                  'AND expires_at IS NOT NULL AND expires_at<=?', (now,)).fetchall():
                db.execute("UPDATE worker_claims SET enabled=1,generation=generation+1,owner='authority',"
                           "reason='drain_expired',expires_at=NULL,updated_at=? WHERE worker=?",
                           (now, row['worker']))
                expired.append((row['worker'], row['owner'], row['generation'] + 1))
        for worker, owner, generation in expired:
            self._record('drain_expired', worker=worker, previous_owner=owner, generation=generation)
            logging.info('drain_expired: %s (was held by %s); worker claims enabled again', worker, owner)
        return [w for w, _, _ in expired]

    # -- migration from authority.json ---------------------------------------------------------

    def sync_file(self, principals):
        """Apply `claim_enabled` from the worker principals of authority.json (startup and SIGHUP).

        No row: import it (owner `import:authority.json`; a False is kept without expiry exactly as
        today). Row exists and the file value is the one last seen: nothing (the row wins; a log
        line says when the two disagree). File value changed since last seen: the operator edited the
        file the old way, so apply it as a claim change by `file:authority.json`. Never raises."""
        now = self.store.clock()
        seen = {}
        for principal in principals:
            if getattr(principal, 'role', None) == 'worker':
                seen[principal.worker] = principal.claim_enabled
        changes = []
        try:
            with self.store.transaction() as db:
                for worker, value in sorted(seen.items()):
                    row = db.execute('SELECT * FROM worker_claims WHERE worker=?', (worker,)).fetchone()
                    if row is None:
                        db.execute('INSERT INTO worker_claims(worker,enabled,generation,owner,reason,'
                                   'expires_at,needs_operator,file_value,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                                   (worker, int(value), 1, IMPORT_OWNER, 'imported claim_enabled', None, 0,
                                    int(value), now))
                        changes.append(('imported', worker, value))
                    elif row['file_value'] is None or bool(row['file_value']) != value:
                        # The file moved. Record the new file value either way.
                        if row['file_value'] is None:
                            db.execute('UPDATE worker_claims SET file_value=? WHERE worker=?', (int(value), worker))
                            continue
                        db.execute('UPDATE worker_claims SET enabled=?,generation=generation+1,owner=?,reason=?,'
                                   'expires_at=NULL,needs_operator=0,file_value=?,updated_at=? WHERE worker=?',
                                   (int(value), FILE_OWNER, 'authority.json claim_enabled edited', int(value),
                                    now, worker))
                        changes.append(('file_edit', worker, value))
                    elif bool(row['enabled']) != value:
                        changes.append(('file_ignored', worker, value))
        except Exception:
            logging.exception('claim_file_sync_failed')
            return []
        for kind, worker, value in changes:
            if kind == 'file_ignored':
                logging.info('claim_enabled_in_file_ignored:%s file=%s; the claims store holds the live value '
                             '(use `workload drain/enable`)', worker, value)
            elif kind == 'file_edit':
                logging.warning('claim_file_edit_applied:%s claim_enabled=%s by %s, no expiry (legacy path; '
                                'prefer `workload drain --until`)', worker, value, FILE_OWNER)
                self._record('file_claim_edit', worker=worker, enabled=value, owner=FILE_OWNER)
            else:
                logging.info('claim_imported:%s claim_enabled=%s', worker, value)
                self._record('claim_imported', worker=worker, enabled=value)
        return changes


def draining(db, now, principals=None):
    """Workers that must not take new work: one set-based query.

    A worker with no claims row falls back to its principal's `claim_enabled` (a store that was not
    started through the authority, and tests)."""
    rows = {r['worker']: r for r in db.execute('SELECT worker,enabled,expires_at,needs_operator FROM worker_claims')}
    out = {w for w, r in rows.items() if _live_drain(r, now)}
    for principal in (principals or {}).values() if isinstance(principals, dict) else (principals or ()):
        if getattr(principal, 'role', None) == 'worker' and principal.worker not in rows \
                and not principal.claim_enabled:
            out.add(principal.worker)
    return out


def describe(row, now):
    """The roster's view of a claim: who holds it and when it ends (None when never claimed)."""
    if row is None:
        return None
    return dict(enabled=bool(row['enabled']), generation=row['generation'], owner=row['owner'],
                reason=row['reason'], expires_in_s=(None if row['expires_at'] is None
                                                    else round(row['expires_at'] - now, 1)),
                needs_operator=bool(row['needs_operator']), draining=_live_drain(row, now))
