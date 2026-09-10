"""Durable workload authority. Every state transition is a SQLite transaction.

The database is the reservation ledger, not an advisory cache. Lease expiration
fences execution but does NOT free host capacity until cleanup is acknowledged.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .model import Limits, WorkloadError, encode, identity, labels, name, resources, submission

TERMINAL = ("succeeded", "failed", "cancelled", "expired")


class WorkloadStore:
    def __init__(self, path, *, handlers, limits=None, clock=time.time):
        self.path = str(path)
        self.handlers = set(handlers)
        self.limits = limits or Limits()
        self.clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA journal_size_limit=16777216")
            db.executescript(Path(__file__).with_name("schema.sql").read_text())

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def recover(self):
        """Call once at service startup, not whenever a client opens the store."""
        with self.transaction() as db:
            db.execute("UPDATE workers SET ready=0")
            self._expire(db, self.clock())

    def _job(self, db, job_id, owner=None):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or (owner is not None and row["owner"] != owner):
            raise WorkloadError("job not found", 404)
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        result["attempts"] = [dict(a) for a in db.execute(
            "SELECT id,worker,boot,host,fence,state,expires FROM attempts WHERE job=? ORDER BY fence",
            (job_id,))]
        return result

    def submit(self, owner, request, *, allowed_handlers=None):
        name(owner, "owner")
        spec = submission(request, self.handlers if allowed_handlers is None else
                          self.handlers.intersection(allowed_handlers), self.limits)
        digest = identity(spec)
        now = self.clock()
        with self.transaction() as db:
            old = db.execute("SELECT id,request_hash FROM jobs WHERE owner=? AND request_key=?",
                             (owner, spec["key"])).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise WorkloadError("idempotency key already names different inputs", 409)
                return self._job(db, old["id"])
            self._expire(db, now)
            self._prune(db, now)
            active = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            total = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
            if active >= self.limits.active_jobs or total >= self.limits.active_jobs + self.limits.terminal_jobs:
                raise WorkloadError("job storage capacity exhausted", 429)
            jid = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,owner,request_key,request_hash,spec,state,created,updated,retain) "
                       "VALUES(?,?,?,?,?,'queued',?,?,?)",
                       (jid, owner, spec["key"], digest, encode(spec), now, now, spec["retain"]))
            return self._job(db, jid)

    def get(self, owner, job_id):
        with self.transaction() as db:
            self._expire(db, self.clock())
            return self._job(db, job_id, owner)

    def list_jobs(self, owner, limit=100):
        limit = max(1, min(int(limit), 20))
        with self.transaction() as db:
            self._expire(db, self.clock())
            ids = [r[0] for r in db.execute("SELECT id FROM jobs WHERE owner=? ORDER BY created DESC LIMIT ?",
                                            (owner, limit))]
            return [self._job(db, jid, owner) for jid in ids]

    def register(self, worker_id, host_id, boot, report, *, cleaned=()):
        """Worker credential binds worker+host at the HTTP boundary.

        cleaned is proof from the worker's local journal/process reconciliation,
        never inferred merely because an attempt vanished from a report.
        """
        for value, field in ((worker_id, "worker"), (host_id, "host"), (boot, "boot")):
            name(value, field)
        if not isinstance(report, dict) or set(report) - {"capacity", "available", "labels", "handlers", "ready"}:
            raise WorkloadError("invalid worker report")
        capacity, available = resources(report.get("capacity")), resources(report.get("available"))
        tags = labels(report.get("labels", {}))
        handlers = report.get("handlers")
        if not isinstance(handlers, list) or not handlers or len(handlers) > 64:
            raise WorkloadError("worker must name installed handlers")
        for handler in handlers:
            if name(handler, "handler") not in self.handlers:
                raise WorkloadError("unknown worker handler")
        if not isinstance(report.get("ready"), bool) or len(cleaned) > self.limits.claims_per_worker:
            raise WorkloadError("invalid readiness or cleanup report")
        body = dict(capacity=capacity, available=available, labels=tags, handlers=sorted(set(handlers)),
                    ready=report["ready"])
        raw = encode(body, self.limits.record_bytes)
        now = self.clock()
        with self.transaction() as db:
            self._expire(db, now)
            old = db.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
            if old and old["host"] != host_id:
                raise WorkloadError("worker identity cannot change physical host", 409)
            if not old and db.execute("SELECT count(*) FROM workers").fetchone()[0] >= self.limits.workers:
                raise WorkloadError("worker capacity exhausted", 429)
            if old and old["boot"] != boot:
                for a in db.execute("SELECT * FROM attempts WHERE worker=? AND state='running'", (worker_id,)):
                    self._abandon(db, a, now, "worker session changed")
            db.execute("INSERT INTO workers(id,host,boot,report,seen,ready) VALUES(?,?,?,?,?,0) "
                       "ON CONFLICT(id) DO UPDATE SET boot=excluded.boot,report=excluded.report,seen=excluded.seen",
                       (worker_id, host_id, boot, raw, now))
            for aid in cleaned:
                row = db.execute("SELECT * FROM attempts WHERE id=? AND worker=?", (aid, worker_id)).fetchone()
                if row is None:
                    raise WorkloadError("unknown cleanup attempt", 409)
                if row["state"] == "running":
                    self._abandon(db, row, now, "worker confirmed process stopped")
                db.execute("UPDATE attempts SET state='ended' WHERE id=? AND state='cleanup'", (aid,))
            dirty = db.execute("SELECT count(*) FROM attempts WHERE worker=? AND state='cleanup'", (worker_id,)).fetchone()[0]
            ready = bool(body["ready"] and not dirty)
            db.execute("UPDATE workers SET ready=? WHERE id=?", (ready, worker_id))
            return {"worker": worker_id, "boot": boot, "ready": ready,
                    "cleanup": [r[0] for r in db.execute("SELECT id FROM attempts WHERE worker=? AND state='cleanup'", (worker_id,))]}

    def _worker(self, db, worker, boot):
        row = db.execute("SELECT * FROM workers WHERE id=? AND boot=?", (worker, boot)).fetchone()
        if row is None:
            raise WorkloadError("worker session is not current", 409)
        return row

    def claim(self, worker, boot):
        from .placement import place
        now = self.clock()
        with self.transaction() as db:
            self._expire(db, now)
            current = self._worker(db, worker, boot)
            if not current["ready"] or now - current["seen"] > self.limits.fresh_seconds:
                return None
            # A lost poll reply must return the SAME assignment until completion.
            existing = db.execute("SELECT * FROM attempts WHERE worker=? AND boot=? AND state='running' ORDER BY created LIMIT 1",
                                  (worker, boot)).fetchone()
            if existing:
                return self._assignment(db, existing)
            place(db, now, self.limits)
            assigned = db.execute("SELECT * FROM attempts WHERE worker=? AND boot=? AND state='running' ORDER BY created LIMIT 1",
                                  (worker, boot)).fetchone()
            return self._assignment(db, assigned) if assigned else None

    def _assignment(self, db, attempt):
        job = self._job(db, attempt["job"])
        return {"job_id": job["id"], "attempt_id": attempt["id"], "fence": attempt["fence"],
                "lease_remaining": max(0, attempt["expires"] - self.clock()),
                "expires": attempt["expires"], "worker": attempt["worker"], "boot": attempt["boot"],
                "owner": job["owner"], "spec": job["spec"]}

    def heartbeat(self, worker, boot, attempt_id, fence):
        now = self.clock()
        with self.transaction() as db:
            self._expire(db, now)
            self._worker(db, worker, boot)
            attempt = db.execute("SELECT * FROM attempts WHERE id=? AND worker=? AND boot=? AND fence=? AND state='running'",
                                 (attempt_id, worker, boot, fence)).fetchone()
            if not attempt:
                raise WorkloadError("execution lease is no longer valid", 409)
            expires = now + self.limits.lease_seconds
            db.execute("UPDATE attempts SET expires=? WHERE id=?", (expires, attempt_id))
            return {"expires": expires, "lease_remaining": self.limits.lease_seconds}

    def complete(self, worker, boot, attempt_id, fence, *, input_digest, outcome, result):
        if outcome not in ("succeeded", "product_failure", "infrastructure"):
            raise WorkloadError("invalid outcome")
        raw = encode({"outcome": outcome, "input_digest": input_digest, "result": result}, self.limits.record_bytes)
        now = self.clock()
        with self.transaction() as db:
            self._expire(db, now)
            self._worker(db, worker, boot)
            a = db.execute("SELECT * FROM attempts WHERE id=? AND worker=? AND boot=? AND fence=?",
                           (attempt_id, worker, boot, fence)).fetchone()
            if not a:
                raise WorkloadError("unknown attempt", 409)
            job = self._job(db, a["job"])
            if job["spec"]["input_digest"] != input_digest:
                raise WorkloadError("result input digest differs from assignment", 409)
            if a["state"] == "ended" and a["result"] == raw:
                return job  # Idempotent acknowledgement, including infrastructure retry.
            if a["state"] != "running" or job["fence"] != fence or job["state"] != "running":
                raise WorkloadError("attempt has been fenced", 409)
            from .artifacts import validate_artifacts
            validate_artifacts(db, job['owner'], result)
            # complete is sent only AFTER owned processes/containers are stopped.
            db.execute("UPDATE attempts SET state='ended',result=? WHERE id=?", (raw, attempt_id))
            state = "succeeded" if outcome == "succeeded" else "failed"
            reason = None
            if outcome == "infrastructure" and fence < self.limits.attempts:
                state, reason = "queued", "infrastructure retry"
            db.execute("UPDATE jobs SET state=?,result=?,reason=?,updated=? WHERE id=?",
                       (state, raw, reason, now, job["id"]))
            return self._job(db, job["id"])

    def cancel(self, owner, job_id):
        with self.transaction() as db:
            job = self._job(db, job_id, owner)
            if job["state"] not in TERMINAL:
                db.execute("UPDATE attempts SET state='cleanup' WHERE job=? AND state='running'", (job_id,))
                db.execute("UPDATE workers SET ready=0 WHERE id IN (SELECT worker FROM attempts WHERE job=? AND state='cleanup')", (job_id,))
                db.execute("UPDATE jobs SET state='cancelled',reason='cancelled by owner',updated=? WHERE id=?",
                           (self.clock(), job_id))
            return self._job(db, job_id)

    def _abandon(self, db, attempt, now, reason):
        db.execute("UPDATE attempts SET state='cleanup' WHERE id=?", (attempt["id"],))
        db.execute("UPDATE workers SET ready=0 WHERE id=?", (attempt["worker"],))
        state = "queued" if attempt["fence"] < self.limits.attempts else "failed"
        db.execute("UPDATE jobs SET state=?,reason=?,updated=? WHERE id=? AND fence=? AND state='running'",
                   (state, reason, now, attempt["job"], attempt["fence"]))

    def _expire(self, db, now):
        for job in list(db.execute("SELECT id,spec,state FROM jobs WHERE state IN ('queued','running')")):
            spec = json.loads(job["spec"])
            deadline = spec.get("deadline")
            if deadline is None:
                continue
            reason = "execution deadline expired"
            if deadline > now:
                estimate = spec["estimate_seconds"]
                if job["state"] != "queued" or deadline - now >= estimate:
                    continue
                reason = ("estimated execution cannot fit remaining deadline "
                          f"({max(0, deadline-now):.0f}s < {estimate:.0f}s)")
            if job["state"] == "running":
                db.execute("UPDATE attempts SET state='cleanup' WHERE job=? AND state='running'", (job["id"],))
                db.execute("UPDATE workers SET ready=0 WHERE id IN "
                           "(SELECT worker FROM attempts WHERE job=? AND state='cleanup')", (job["id"],))
            db.execute("UPDATE jobs SET state='expired',reason=?,updated=? "
                       "WHERE id=? AND state IN ('queued','running')", (reason, now, job["id"]))
        for a in list(db.execute("SELECT * FROM attempts WHERE state='running' AND expires<=?", (now,))):
            self._abandon(db, a, now, "execution lease expired")

    def _prune(self, db, now):
        if self.limits.terminal_seconds is None:
            return  # Missing destructive window fails closed; submission still enforces a hard cap.
        rows = db.execute("SELECT id,updated,retain FROM jobs WHERE state IN ('succeeded','failed','cancelled','expired') "
                          "AND NOT EXISTS (SELECT 1 FROM attempts WHERE job=jobs.id AND state IN ('running','cleanup')) "
                          "ORDER BY updated DESC").fetchall()
        for index, row in enumerate(rows):
            if not row["retain"] and (index >= self.limits.terminal_jobs or now-row["updated"] > self.limits.terminal_seconds):
                db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))

    def sweep(self):
        with self.transaction() as db:
            self._expire(db, self.clock())
            self._prune(db, self.clock())
        with closing(self.connect()) as db:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
