"""Bounded broker dispatch intents; durable uncertainty is never an authorization."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import stat

from .fleet_operations import OperationStore


class BrokerOwnerJournal(OperationStore):
    def __init__(self, path, source_sha256, incarnation):
        if not isinstance(source_sha256, str) or not re.fullmatch('[a-f0-9]{64}', source_sha256) or not isinstance(incarnation, str) or not 1 <= len(incarnation) <= 192:
            raise ValueError('broker_journal_exact_source_incarnation_required')
        target = Path(path)
        if not target.is_absolute() or target.resolve() != target:
            raise PermissionError('broker_journal_absolute_private_path_required')
        parent = target.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise PermissionError('broker_journal_private_parent_required')
        fd = os.open(target, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError('broker_journal_private_file_required')
        finally:
            os.close(fd)
        super().__init__(str(target), max_records=64, max_age_s=None)
        self.source_sha256, self.incarnation = source_sha256, incarnation
        with self._writer() as db:
            db.execute('CREATE TABLE IF NOT EXISTS broker_owner_source (id INTEGER PRIMARY KEY CHECK(id=1), source_sha256 TEXT NOT NULL)')
            row = db.execute('SELECT source_sha256 FROM broker_owner_source WHERE id=1').fetchone()
            if row is not None and row[0] != source_sha256:
                raise ValueError('broker_journal_source_changed')
            db.execute('INSERT OR IGNORE INTO broker_owner_source VALUES(1,?)', (source_sha256,))
            db.execute("CREATE TABLE IF NOT EXISTS broker_owner_effects (nonce TEXT PRIMARY KEY, incarnation TEXT NOT NULL, identity TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('unknown','local-settled','not-dispatched')))")

    def _connect(self):
        db = super()._connect()
        db.execute('PRAGMA synchronous=FULL')
        return db

    @staticmethod
    def _identity(value):
        if not isinstance(value, dict) or set(value) != {'kind', 'peerId', 'requestSha256'} or value['kind'] not in ('warm', 'evict', 'reclaim', 'local-work'):
            raise ValueError('broker_journal_closed_effect_required')
        if not isinstance(value['peerId'], str) or not 1 <= len(value['peerId']) <= 192 or not isinstance(value['requestSha256'], str) or not re.fullmatch('[a-f0-9]{64}', value['requestSha256']):
            raise ValueError('broker_journal_exact_effect_identity_required')
        return json.dumps(value, sort_keys=True, separators=(',', ':'))

    def intent(self, nonce, identity):
        if not isinstance(nonce, str) or not re.fullmatch('[a-f0-9]{32}', nonce):
            raise ValueError('broker_journal_nonce_required')
        encoded = self._identity(identity)
        with self._writer() as db:
            if db.execute('SELECT nonce FROM broker_owner_effects WHERE nonce=?', (nonce,)).fetchone() is not None:
                # Replaying a submitted nonce is observation, never dispatch.
                raise ValueError('broker_journal_existing_intent_requires_reconciliation')
            if db.execute('SELECT count(*) FROM broker_owner_effects').fetchone()[0] >= 64:
                raise ValueError('broker_journal_record_bound')
            db.execute("INSERT INTO broker_owner_effects VALUES(?,?,?,'unknown')", (nonce, self.incarnation, encoded))

    def settle_local(self, nonce, identity):
        if identity.get('kind') != 'local-work':
            raise ValueError('broker_remote_reply_is_not_physical_settlement')
        return self._finish(nonce, identity, 'local-settled')

    def known_not_dispatched(self, nonce, identity):
        return self._finish(nonce, identity, 'not-dispatched')

    def _finish(self, nonce, identity, state):
        encoded = self._identity(identity)
        with self._writer() as db:
            changed = db.execute("UPDATE broker_owner_effects SET state=? WHERE nonce=? AND incarnation=? AND identity=? AND state='unknown'", (state, nonce, self.incarnation, encoded)).rowcount
            if changed != 1:
                raise ValueError('broker_journal_settlement_cas_refused')

    def unsettled(self):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute("SELECT nonce,incarnation,identity,state FROM broker_owner_effects WHERE state='unknown' ORDER BY nonce")]
