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

from .model import (Limits, failure_signature, WorkloadError, encode, host_view, identity, labels, name, resources,
                    submission)
from .model import progress as validate_progress

TERMINAL = ("succeeded", "failed", "cancelled", "expired")


def _limit_breach(result, need):
    """The job's own resource limit, when the run ended by breaching it."""
    resources = (result or {}).get("resources") or {}
    try:
        need = json.loads(need) if isinstance(need, str) else (need or {})
    except ValueError:
        need = {}
    if resources.get("oom_kill", 0) > 0:
        return ("resource limit: memory peak %s bytes reached the job's declared need.memory_bytes %s; "
                "raise the job's need, a retry would hit the same cap"
                % (resources.get("memory_peak_bytes", "unknown"), need.get("memory_bytes", need.get("ram", "unknown"))))
    if resources.get("pids_max_events", 0) > 0:
        return ("resource limit: the attempt reached its task limit (peak %s); raise the handler's max_tasks, "
                "a retry would hit the same cap" % resources.get("tasks_peak", "unknown"))
    return None


class WorkloadStore:
    def __init__(self, path, *, handlers, limits=None, clock=time.time, compilation_policy=None,
                 execution_providers=None, remote_hosts=None):
        self.path = str(path)
        self.handlers = set(handlers)
        self.limits = limits or Limits()
        self.clock = clock
        self.compilation_policy = compilation_policy
        self.execution_providers = dict(execution_providers or {})
        self.remote_hosts = dict(remote_hosts or {})
        if (set(self.execution_providers) - self.handlers or
                any(not isinstance(p, str) or not p for p in self.execution_providers.values())):
            raise ValueError('invalid configured remote handler mapping')
        # Principals are bound by the HTTP server (service.py / WorkloadServer).
        # A standalone store has no caps and behaves exactly as before binding.
        self.principals = {}
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA journal_size_limit=16777216")
            db.executescript(Path(__file__).with_name("schema.sql").read_text())
            # Additive migrations for databases created before these columns.
            if "labels" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN labels TEXT NOT NULL DEFAULT '{}'")
            if "progress" not in {row[1] for row in db.execute("PRAGMA table_info(attempts)")}:
                db.execute("ALTER TABLE attempts ADD COLUMN progress TEXT")
            if "compilation" not in {row[1] for row in db.execute("PRAGMA table_info(attempts)")}:
                db.execute("ALTER TABLE attempts ADD COLUMN compilation TEXT")

    def bind_principals(self, principals):
        """The caller-principal table, for per-principal caps and the job list."""
        self.principals = {p.id: p for p in principals}

    def reserve_remote_dispatch(self, provider, slots):
        """Reserve a configured GitHub provider slot and return one outbox row.

        The reservation is committed before the caller makes network I/O. A
        process restart changes stale `dispatching` rows to `dispatch_unknown`;
        it never blindly sends a duplicate workflow_dispatch.
        """
        now = self.clock()
        with self.transaction() as db:
            self._expire(db, now)
            db.execute("UPDATE github_remote_jobs SET state='terminal',reason='job ended before dispatch',updated=? "
                       "WHERE provider=? AND state='queued' AND job IN "
                       "(SELECT id FROM jobs WHERE state IN ('succeeded','failed','cancelled','expired'))",
                       (now, provider))
            active = db.execute("SELECT count(*) FROM github_remote_jobs WHERE provider=? "
                                "AND state IN ('dispatching','dispatch_unknown','running','cancel_requested')",
                                (provider,)).fetchone()[0]
            if active >= slots:
                return None
            row = db.execute("SELECT r.job,r.correlation,j.spec FROM github_remote_jobs r "
                             "JOIN jobs j ON j.id=r.job WHERE r.provider=? AND r.state='queued' "
                             "AND j.state='queued' ORDER BY j.created,j.id LIMIT 1", (provider,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE github_remote_jobs SET state='dispatching',dispatch_started=?,updated=? "
                       "WHERE job=? AND state='queued'", (now, now, row['job']))
            return dict(job_id=row['job'], correlation=row['correlation'], spec=json.loads(row['spec']))

    def remote_dispatch_unknown(self, job_id, *, reason=None):
        with self.transaction() as db:
            row = db.execute("SELECT state FROM github_remote_jobs WHERE job=?", (job_id,)).fetchone()
            if row is None:
                raise WorkloadError('unknown remote dispatch', 404)
            if row['state'] == 'dispatching':
                db.execute("UPDATE github_remote_jobs SET state='dispatch_unknown',reason=?,updated=? WHERE job=?",
                           (reason, self.clock(), job_id))

    def remote_dispatch_reconcile(self, provider, *, stale_after=30):
        """Return bounded dispatch rows awaiting run lookup and age abandoned sends."""
        now = self.clock()
        with self.transaction() as db:
            db.execute("UPDATE github_remote_jobs SET state='dispatch_unknown',reason='dispatch acknowledgement lost',updated=? "
                       "WHERE provider=? AND state='dispatching' AND dispatch_started<=?",
                       (now, provider, now-stale_after))
            rows = db.execute("SELECT r.job,r.correlation,r.state,r.run_id,r.run_attempt,r.run_status,r.reason,"
                              "j.state AS job_state,j.spec,j.fence FROM github_remote_jobs r "
                              "JOIN jobs j ON j.id=r.job WHERE r.provider=? AND r.state!='terminal' "
                              "AND r.state!='refused' ORDER BY r.created,r.job LIMIT 64", (provider,)).fetchall()
            return [dict(row, spec=json.loads(row['spec'])) for row in rows]

    def remote_bind_run(self, provider, job_id, correlation, run_id, run_attempt, *, input_digest=None, release_key=None):
        if (not isinstance(run_id, str) or not run_id.isdigit() or type(run_attempt) is not int or
                run_attempt != 1):
            raise WorkloadError('github_run_identity_invalid', 403)
        if (input_digest is None) != (release_key is None):
            raise WorkloadError('github_remote_release_identity_invalid', 400)
        now = self.clock()
        with self.transaction() as db:
            row = db.execute("SELECT r.*,j.spec AS job_spec FROM github_remote_jobs r "
                             "JOIN jobs j ON j.id=r.job WHERE r.job=?", (job_id,)).fetchone()
            if row is None or row['provider'] != provider or row['correlation'] != correlation:
                raise WorkloadError('github_remote_correlation_mismatch', 403)
            if row['state'] in ('terminal','refused'):
                raise WorkloadError('github_remote_attempt_not_live', 409)
            if input_digest is not None:
                spec = json.loads(row['job_spec'])
                if spec.get('input_digest') != input_digest or spec.get('key') != release_key:
                    raise WorkloadError('github_remote_release_identity_mismatch', 403)
            other = db.execute("SELECT job FROM github_remote_jobs WHERE provider=? AND run_id=? AND job!=?",
                               (provider, run_id, job_id)).fetchone()
            if other:
                raise WorkloadError('github_run_already_bound', 409)
            if row['run_id'] is not None and (row['run_id'] != run_id or row['run_attempt'] != run_attempt):
                raise WorkloadError('github_run_replay_refused', 409)
            state = 'cancel_requested' if row['state'] == 'cancel_requested' else 'running'
            db.execute("UPDATE github_remote_jobs SET state=?,run_id=?,run_attempt=?,run_status='in_progress',"
                       "reason=NULL,updated=? WHERE job=?", (state, run_id, run_attempt, now, job_id))
            return self._job(db, job_id)

    def remote_job_context(self, provider, job_id, correlation):
        with self.transaction() as db:
            self._expire(db, self.clock())
            row = db.execute("SELECT r.*,j.state AS job_state,j.spec,j.owner,j.fence FROM github_remote_jobs r "
                             "JOIN jobs j ON j.id=r.job WHERE r.job=?", (job_id,)).fetchone()
            if row is None or row['provider'] != provider or row['correlation'] != correlation:
                raise WorkloadError('github_remote_correlation_mismatch', 403)
            if row['state'] not in ('running',) or row['job_state'] != 'queued':
                raise WorkloadError('github_remote_job_not_claimable', 409)
            return dict(job_id=job_id, owner=row['owner'], fence=row['fence'], spec=json.loads(row['spec']),
                        correlation=row['correlation'], provider=row['provider'], run_id=row['run_id'],
                        run_attempt=row['run_attempt'])

    def remote_job_status(self, provider, job_id):
        with self.transaction() as db:
            self._expire(db, self.clock())
            row = db.execute("SELECT provider FROM github_remote_jobs WHERE job=?", (job_id,)).fetchone()
            if row is None or row['provider'] != provider:
                raise WorkloadError('github_remote_job_not_found', 404)
            return self._job(db, job_id)

    def remote_cancel_requests(self, provider):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT job,correlation,run_id,run_attempt,run_status FROM github_remote_jobs "
                "WHERE provider=? AND state='cancel_requested' ORDER BY created LIMIT 64", (provider,))]

    def remote_run_status(self, provider, job_id, run_id, status, conclusion=None):
        if status not in ('queued','in_progress','completed'):
            raise WorkloadError('github_run_status_invalid', 400)
        now = self.clock()
        with self.transaction() as db:
            row = db.execute("SELECT r.*,j.state AS job_state FROM github_remote_jobs r "
                             "JOIN jobs j ON j.id=r.job WHERE r.job=?", (job_id,)).fetchone()
            if row is None or row['provider'] != provider or row['run_id'] != run_id:
                raise WorkloadError('github_run_status_mismatch', 409)
            db.execute("UPDATE github_remote_jobs SET run_status=?,updated=? WHERE job=?", (status, now, job_id))
            if status == 'completed' and row['job_state'] not in TERMINAL:
                reason = 'GitHub Actions run ended without a Harmony completion'
                if conclusion:
                    reason += ' ('+str(conclusion)[:64]+')'
                raw = self._terminal_result(db, job_id, reason)
                db.execute("UPDATE attempts SET state='cleanup',result=COALESCE(result,?) WHERE job=? AND state='running'",
                           (raw, job_id))
                db.execute("UPDATE workers SET ready=0 WHERE id IN "
                           "(SELECT worker FROM attempts WHERE job=? AND state='cleanup')", (job_id,))
                db.execute("UPDATE jobs SET state='failed',reason=?,result=?,updated=? WHERE id=? "
                           "AND state IN ('queued','running')", (reason, raw, now, job_id))
            cleanup = db.execute("SELECT count(*) FROM attempts WHERE job=? AND state IN ('running','cleanup')",
                                 (job_id,)).fetchone()[0]
            if status == 'completed' and cleanup == 0:
                db.execute("UPDATE github_remote_jobs SET state='terminal',reason=?,updated=? WHERE job=?",
                           (conclusion or 'completed', now, job_id))
            return self._job(db, job_id)

    def remote_finalize_cleanup(self):
        now = self.clock()
        with self.transaction() as db:
            db.execute("UPDATE github_remote_jobs SET state='terminal',updated=? WHERE state IN ('running','cancel_requested') "
                       "AND run_status='completed' AND NOT EXISTS "
                       "(SELECT 1 FROM attempts a WHERE a.job=github_remote_jobs.job AND a.state IN ('running','cleanup'))",
                       (now,))

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
        result["labels"] = json.loads(result["labels"] or "{}")
        # Only the latest reported progress is kept; absent means never reported.
        latest = db.execute("SELECT progress FROM attempts WHERE job=? AND progress IS NOT NULL "
                            "ORDER BY fence DESC LIMIT 1", (job_id,)).fetchone()
        if latest:
            result["progress"] = json.loads(latest["progress"])
        result["spec"] = json.loads(result["spec"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        # What placement avoids on a retry, and what a caller creating the
        # NEXT job after this one failed carries forward. None unless the last
        # verdict was an infrastructure outcome.
        result["failure_signature"] = failure_signature(result["result"])
        result["attempts"] = [dict(a) for a in db.execute(
            "SELECT id,worker,boot,host,fence,state,expires,compilation FROM attempts WHERE job=? ORDER BY fence",
            (job_id,))]
        for attempt in result['attempts']:
            attempt['compilation'] = json.loads(attempt['compilation']) if attempt['compilation'] else None
        remote = db.execute("SELECT provider,correlation,state,run_id,run_attempt,run_status,reason "
                            "FROM github_remote_jobs WHERE job=?", (job_id,)).fetchone()
        if remote:
            result['remote_execution'] = dict(remote)
        return result

    def _running(self, db, owner):
        return db.execute("SELECT count(*) FROM attempts a JOIN jobs j ON a.job=j.id "
                          "WHERE j.owner=? AND a.state='running'", (owner,)).fetchone()[0]

    def principal_status(self, owner):
        """The caller's cap and current running count for the job list (J.4)."""
        principal = self.principals.get(owner)
        with self.transaction() as db:
            self._expire(db, self.clock())
            return {"max_running": principal.max_running if principal else None,
                    "running": self._running(db, owner)}

    def submit(self, owner, request, *, allowed_handlers=None):
        name(owner, "owner")
        spec = submission(request, self.handlers if allowed_handlers is None else
                          self.handlers.intersection(allowed_handlers), self.limits)
        provider = self.execution_providers.get(spec['handler'])
        if provider is not None:
            selector = dict(spec['selector'])
            reserved = 'harmony.execution.provider'
            if reserved in selector and selector[reserved] != provider:
                raise WorkloadError('execution provider is operator configured', 403)
            selector[reserved] = provider
            spec['selector'] = selector
            spec['execution_provider'] = provider
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
            principal = self.principals.get(owner)
            if (principal is not None and principal.on_cap == "refuse"
                    and principal.max_running is not None):
                running = self._running(db, owner)
                if running >= principal.max_running:
                    raise WorkloadError(f"principal at max_running ({running})", 429)
            active = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            total = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
            if active >= self.limits.active_jobs or total >= self.limits.active_jobs + self.limits.terminal_jobs:
                raise WorkloadError("job storage capacity exhausted", 429)
            jid = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,owner,request_key,request_hash,spec,state,created,updated,labels,retain) "
                       "VALUES(?,?,?,?,?,'queued',?,?,?,?)",
                       (jid, owner, spec["key"], digest, encode(spec), now, now,
                        encode(spec.get("labels", {})), spec["retain"]))
            if provider is not None:
                db.execute("INSERT INTO github_remote_jobs(job,provider,correlation,state,created,updated) "
                           "VALUES(?,?,?,'queued',?,?)",
                           (jid, provider, uuid.uuid4().hex, now, now))
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
        if self.compilation_policy is not None and host_id not in self.remote_hosts.values():
            host_id = self.compilation_policy.physical_host(host_id, self.clock())
        if not isinstance(report, dict) or set(report) - {"capacity", "available", "labels", "handlers", "ready", "host"}:
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
        if report.get("host") is not None:
            body["host"] = host_view(report["host"])
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
                # Structural backstop: no attempt row may end without a result,
                # whatever path moved it to cleanup.
                db.execute("UPDATE attempts SET state='ended',result=COALESCE(result,?) WHERE id=? AND state='cleanup'",
                           (self._terminal_result(db, row["job"], "cleanup ended without a worker result"), aid))
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

    def claim(self, worker, boot, *, job_id=None):
        from .placement import place
        now = self.clock()
        with self.transaction() as db:
            self._expire(db, now)
            current = self._worker(db, worker, boot)
            if not current["ready"] or now - current["seen"] > self.limits.fresh_seconds:
                return None
            # A lost poll reply must return the SAME assignment until completion.
            query = "SELECT * FROM attempts WHERE worker=? AND boot=? AND state='running'"
            params = [worker, boot]
            if job_id is not None:
                query += ' AND job=?'
                params.append(job_id)
            existing = db.execute(query+' ORDER BY created LIMIT 1', params).fetchone()
            if existing:
                return self._assignment(db, existing)
            place(db, now, self.limits, self.principals, self.compilation_policy, only_job_id=job_id)
            query = "SELECT * FROM attempts WHERE worker=? AND boot=? AND state='running'"
            params = [worker, boot]
            if job_id is not None:
                query += ' AND job=?'
                params.append(job_id)
            assigned = db.execute(query+' ORDER BY created LIMIT 1', params).fetchone()
            return self._assignment(db, assigned) if assigned else None

    def _assignment(self, db, attempt):
        job = self._job(db, attempt["job"])
        return {"job_id": job["id"], "attempt_id": attempt["id"], "fence": attempt["fence"],
                "lease_remaining": max(0, attempt["expires"] - self.clock()),
                "expires": attempt["expires"], "worker": attempt["worker"], "boot": attempt["boot"],
                "owner": job["owner"], "spec": job["spec"],
                "compilation": json.loads(attempt['compilation']) if attempt['compilation'] else None}

    def _compilation_live(self, db, attempt, now):
        receipt = json.loads(attempt['compilation']) if attempt['compilation'] else None
        job = db.execute('SELECT spec FROM jobs WHERE id=?', (attempt['job'],)).fetchone()
        spec = json.loads(job['spec'])
        required = self.compilation_policy and self.compilation_policy.required(spec['handler'])
        if required or receipt:
            if self.compilation_policy is None or receipt is None:
                raise WorkloadError('compilation_policy_grant_missing', 409)
            # authorize() raises when the host lost any class this handler
            # needs (narrowing, host removal, expiry). A new revision that still
            # grants every admitted class is a widening or a no-op for this
            # attempt: it keeps running, and its receipt keeps the ADMITTING
            # revision as evidence. Bumping the revision used to kill every
            # running compile attempt fleet-wide (2026-10-02, four e2e attempts).
            current = self.compilation_policy.authorize(attempt['host'], spec['handler'], now).receipt()
            if any(current[key] != receipt.get(key) for key in ('version', 'host', 'classes')):
                raise WorkloadError('compilation_policy_grant_changed', 409)
        return spec, receipt

    def verify_compilation(self, worker, boot, attempt_id, fence, *, input_digest, compilation_class):
        now = self.clock()
        with self.transaction() as db:
            self._expire(db, now)
            self._worker(db, worker, boot)
            attempt = db.execute("SELECT * FROM attempts WHERE id=? AND worker=? AND boot=? "
                                 "AND fence=? AND state='running'", (attempt_id, worker, boot, fence)).fetchone()
            if attempt is None:
                raise WorkloadError('compilation_attempt_not_live', 409)
            spec, receipt = self._compilation_live(db, attempt, now)
            if receipt is None or compilation_class not in receipt['classes']:
                raise WorkloadError('compilation_class_not_reserved', 403)
            if spec['input_digest'] != input_digest:
                raise WorkloadError('compilation_input_mismatch', 409)
            return dict(**receipt, job_id=attempt['job'], attempt_id=attempt_id, fence=fence,
                        worker=worker, boot=boot, input_digest=input_digest,
                        resources=json.loads(attempt['need']), execution_resources=spec['need'],
                        expires=attempt['expires'])

    def heartbeat(self, worker, boot, attempt_id, fence, *, progress=None):
        now = self.clock()
        latest = encode(validate_progress(progress), self.limits.record_bytes) if progress is not None else None
        with self.transaction() as db:
            self._expire(db, now)
            self._worker(db, worker, boot)
            attempt = db.execute("SELECT * FROM attempts WHERE id=? AND worker=? AND boot=? AND fence=? AND state='running'",
                                 (attempt_id, worker, boot, fence)).fetchone()
            if not attempt:
                raise WorkloadError("execution lease is no longer valid", 409)
            self._compilation_live(db, attempt, now)
            expires = now + self.limits.lease_seconds
            # A heartbeat without progress leaves the last reported value in
            # place; progress is an overwrite, never an append.
            db.execute("UPDATE attempts SET expires=?,progress=COALESCE(?,progress) WHERE id=?",
                       (expires, latest, attempt_id))
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
            breach = _limit_breach(result, a["need"])
            if outcome == "infrastructure" and breach:
                # The attempt hit a limit the JOB declared (its own need is the
                # execution cap). A retry runs the same spec into the same cap,
                # and near the line only by luck gets through: end the job and
                # name the fix. Not a product failure; still no retry.
                reason = breach
            elif outcome == "infrastructure" and fence < self.limits.attempts:
                state, reason = "queued", "infrastructure retry"
            db.execute("UPDATE jobs SET state=?,result=?,reason=?,updated=? WHERE id=?",
                       (state, raw, reason, now, job["id"]))
            return self._job(db, job["id"])

    def cancel(self, owner, job_id):
        with self.transaction() as db:
            job = self._job(db, job_id, owner)
            if job["state"] not in TERMINAL:
                raw = self._terminal_result(db, job_id, "cancelled by owner")
                db.execute("UPDATE attempts SET state='cleanup',result=COALESCE(result,?) WHERE job=? AND state='running'",
                           (raw, job_id))
                db.execute("UPDATE workers SET ready=0 WHERE id IN (SELECT worker FROM attempts WHERE job=? AND state='cleanup')", (job_id,))
                db.execute("UPDATE jobs SET state='cancelled',reason='cancelled by owner',result=?,updated=? WHERE id=?",
                           (raw, self.clock(), job_id))
                remote = db.execute("SELECT state FROM github_remote_jobs WHERE job=?", (job_id,)).fetchone()
                if remote:
                    state = 'terminal' if remote['state'] == 'queued' else 'cancel_requested'
                    db.execute("UPDATE github_remote_jobs SET state=?,reason='job cancelled',updated=? WHERE job=? "
                               "AND state!='terminal'", (state, self.clock(), job_id))
            return self._job(db, job_id)

    def withdraw(self, owner, job_id):
        """Owner cancel that only ever ends a job NO worker has attempted.

        `cancel` fences a running attempt and holds its worker in cleanup, so a
        caller that merely changed its mind about WHICH input to run (a newer
        snapshot superseded a queued job) must not race a claim with it. This
        decides inside the same IMMEDIATE transaction that `claim` takes: a
        job that is queued with zero attempts ends `cancelled`; any other job is
        returned unchanged, and the caller reads `state` to learn which.
        """
        with self.transaction() as db:
            job = self._job(db, job_id, owner)
            attempted = db.execute("SELECT count(*) FROM attempts WHERE job=?", (job_id,)).fetchone()[0]
            if job["state"] == "queued" and attempted == 0:
                raw = self._terminal_result(db, job_id, "withdrawn by owner before any attempt")
                db.execute("UPDATE jobs SET state='cancelled',reason='withdrawn by owner before any attempt',result=?,updated=? WHERE id=?",
                           (raw, self.clock(), job_id))
                db.execute("UPDATE github_remote_jobs SET state='terminal',reason='job withdrawn before dispatch',updated=? "
                           "WHERE job=? AND state='queued'", (self.clock(), job_id))
            return self._job(db, job_id)

    def _terminal_result(self, db, job_id, reason):
        """The verdict every terminal row must carry. Absence and failure must
        never look alike: an abandoned attempt names what ended it."""
        spec = json.loads(db.execute("SELECT spec FROM jobs WHERE id=?", (job_id,)).fetchone()["spec"])
        return encode({"outcome": "infrastructure", "input_digest": spec.get("input_digest"),
                       "result": {"error": "abandoned", "detail": reason, "artifacts": []}},
                      self.limits.record_bytes)

    def _abandon(self, db, attempt, now, reason):
        raw = self._terminal_result(db, attempt["job"], reason)
        # COALESCE: the first terminal reason for an attempt is the true one.
        db.execute("UPDATE attempts SET state='cleanup',result=COALESCE(result,?) WHERE id=?", (raw, attempt["id"]))
        db.execute("UPDATE workers SET ready=0 WHERE id=?", (attempt["worker"],))
        state = "queued" if attempt["fence"] < self.limits.attempts else "failed"
        db.execute("UPDATE jobs SET state=?,reason=?,result=?,updated=? WHERE id=? AND fence=? AND state='running'",
                   (state, reason, raw, now, attempt["job"], attempt["fence"]))

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
            raw = self._terminal_result(db, job["id"], reason)
            if job["state"] == "running":
                db.execute("UPDATE attempts SET state='cleanup',result=COALESCE(result,?) WHERE job=? AND state='running'",
                           (raw, job["id"]))
                db.execute("UPDATE workers SET ready=0 WHERE id IN "
                           "(SELECT worker FROM attempts WHERE job=? AND state='cleanup')", (job["id"],))
            db.execute("UPDATE github_remote_jobs SET state=CASE WHEN state='queued' THEN 'terminal' "
                       "ELSE 'cancel_requested' END,reason=?,updated=? WHERE job=? AND state!='terminal'",
                       (reason, now, job['id']))
            db.execute("UPDATE jobs SET state='expired',reason=?,result=?,updated=? "
                       "WHERE id=? AND state IN ('queued','running')", (reason, raw, now, job["id"]))
        for a in list(db.execute("SELECT * FROM attempts WHERE state='running' AND expires<=?", (now,))):
            self._abandon(db, a, now, "execution lease expired")

    def _prune(self, db, now):
        if self.limits.terminal_seconds is None:
            return  # Missing destructive window fails closed; submission still enforces a hard cap.
        rows = db.execute("SELECT id,updated,retain FROM jobs WHERE state IN ('succeeded','failed','cancelled','expired') "
                          "AND NOT EXISTS (SELECT 1 FROM attempts WHERE job=jobs.id AND state IN ('running','cleanup')) "
                          "AND NOT EXISTS (SELECT 1 FROM github_remote_jobs WHERE job=jobs.id "
                          "AND state NOT IN ('terminal','refused')) "
                          "ORDER BY updated DESC").fetchall()
        for index, row in enumerate(rows):
            if not row["retain"] and (index >= self.limits.terminal_jobs or now-row["updated"] > self.limits.terminal_seconds):
                db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))

    def sweep(self):
        with self.transaction() as db:
            self._expire(db, self.clock())
            self._prune(db, self.clock())
            db.execute("UPDATE github_remote_jobs SET state='terminal',updated=? WHERE state IN ('running','cancel_requested') "
                       "AND run_status='completed' AND NOT EXISTS "
                       "(SELECT 1 FROM attempts a WHERE a.job=github_remote_jobs.job AND a.state IN ('running','cleanup'))",
                       (self.clock(),))
        with closing(self.connect()) as db:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
