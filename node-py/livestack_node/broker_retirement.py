"""Durable first-adoption abort intent; accepted legacy results stay unknown."""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import stat

from .broker_predecessor import inspect_predecessor, validate_abort_plan, verify_retained_files, verify_terminated


class BrokerRetirementJournal:
    def __init__(self, path, plan, registered_inventory):
        self.plan = json.loads(json.dumps(validate_abort_plan(plan, registered_inventory)))
        self.inventory = json.loads(json.dumps(registered_inventory))
        self._bound_digest = sha256(json.dumps(self.plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.path = Path(path)
        parent = self.path.parent.stat()
        if not self.path.is_absolute() or self.path.resolve() != self.path or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise PermissionError('broker_private_retirement_journal_required')
        fd = os.open(self.path, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise PermissionError('broker_private_retirement_journal_required')
        finally:
            os.close(fd)
        with closing(self._connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS binding(id INTEGER PRIMARY KEY CHECK(id=1), plan TEXT NOT NULL)')
            row = db.execute('SELECT plan FROM binding WHERE id=1').fetchone()
            if row is not None and row[0] != self.digest:
                raise ValueError('broker_retirement_plan_changed')
            db.execute('INSERT OR IGNORE INTO binding VALUES(1,?)', (self.digest,))
            db.execute("CREATE TABLE IF NOT EXISTS units(unit TEXT PRIMARY KEY, witness TEXT NOT NULL, permission TEXT NOT NULL, phase TEXT NOT NULL CHECK(phase IN ('submitted','terminated')), terminal TEXT)")
            db.commit()

    @property
    def digest(self):
        return self._bound_digest

    def _verify_binding(self):
        validate_abort_plan(self.plan, self.inventory)
        current = sha256(json.dumps(self.plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if current != self._bound_digest:
            raise ValueError('broker_retirement_loaded_plan_changed')
        with closing(self._connect()) as db:
            row = db.execute('SELECT plan FROM binding WHERE id=1').fetchone()
        if row != (self._bound_digest,):
            raise ValueError('broker_retirement_durable_binding_changed')

    def _check_transaction_binding(self, db):
        validate_abort_plan(self.plan, self.inventory)
        current = sha256(json.dumps(self.plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if current != self._bound_digest or db.execute('SELECT plan FROM binding WHERE id=1').fetchone() != (self._bound_digest,):
            raise ValueError('broker_retirement_transaction_binding_changed')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def submit(self, unit, permission):
        """Caller freshly authenticates the registered canonical transport.

        Permission receipt is retained for audit, never trusted as physical
        settlement. Returning a new witness authorizes exactly ONE dispatch;
        an existing intent always requires read-only reconciliation.
        """
        self._verify_binding()
        selected = next((row for row in self.plan['units'] if row['unit'] == unit), None)
        if selected is None:
            raise ValueError('broker_registered_abort_unit_required')
        verify_retained_files(self.plan, self.inventory)
        witness = inspect_predecessor(selected, self.plan['retainedFiles'])
        if not isinstance(permission, dict) or set(permission) != {'ok','permissionOnly','principal','nonce','requestSha256','authorityMachine','keeperSourceSha256'} or permission.get('ok') is not True or permission.get('permissionOnly') is not True:
            raise PermissionError('broker_canonical_permission_receipt_required')
        self._verify_binding()
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            self._check_transaction_binding(db)
            if db.execute('SELECT unit FROM units WHERE unit=?', (unit,)).fetchone() is not None:
                raise ValueError('broker_existing_abort_intent_reconcile_only')
            db.execute("INSERT INTO units VALUES(?,?,?,'submitted',NULL)",
                       (unit, json.dumps(witness, sort_keys=True), json.dumps(permission, sort_keys=True)))
            self._check_transaction_binding(db)
            db.commit()
        return {'planSha256': self.digest, 'unit': unit, 'phase': 'submitted',
                'historicalCause': 'unknown', 'acceptedWorkOutcome': 'unknown',
                'intentRecordedOnly': True, 'authorityGranted': False}

    def reconcile(self, unit):
        self._verify_binding()
        selected = next((row for row in self.plan['units'] if row['unit'] == unit), None)
        if selected is None:
            raise ValueError('broker_registered_abort_unit_required')
        with closing(self._connect()) as db:
            row = db.execute('SELECT witness FROM units WHERE unit=?', (unit,)).fetchone()
        if row is None:
            raise ValueError('broker_submitted_abort_intent_required')
        # Always reread kernel and manager; retained terminal receipt is not a
        # current physical claim after a restart or a changed incarnation.
        terminal = verify_terminated(selected, json.loads(row[0]), self.plan['retainedFiles'])
        self._verify_binding()
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            self._check_transaction_binding(db)
            db.execute("UPDATE units SET phase='terminated',terminal=? WHERE unit=?", (json.dumps(terminal, sort_keys=True), unit))
            self._check_transaction_binding(db)
            db.commit()
        return {'planSha256': self.digest, **terminal, 'activationAllowed': False,
                'finalRetentionRequired': True}

    def status(self):
        self._verify_binding()
        with closing(self._connect()) as db:
            rows = db.execute('SELECT unit,phase FROM units ORDER BY unit').fetchall()
        return {'planSha256': self.digest, 'units': [{'unit': unit, 'phase': phase} for unit, phase in rows],
                'activationAllowed': False, 'jobsCompleted': False}
