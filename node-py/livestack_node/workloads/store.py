"""Durable workload authority. Every state transition is a SQLite transaction.

The database is the reservation ledger, not an advisory cache. Lease expiration
fences execution but does NOT free host capacity until cleanup is acknowledged.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .model import (Limits, failure_signature, WorkloadError, encode, host_view, identity, labels, name, resources,
                    submission)
from .environment_receipts import validate as validate_environment_receipt
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
                 execution_providers=None, remote_hosts=None, environment_handlers=None):
        self.path = str(path)
        self.handlers = set(handlers)
        self.limits = limits or Limits()
        self.clock = clock
        self.compilation_policy = compilation_policy
        self.execution_providers = dict(execution_providers or {})
        self.remote_hosts = dict(remote_hosts or {})
        self.environment_handlers = {}
        for handler, policy in dict(environment_handlers or {}).items():
            if handler not in self.handlers or not isinstance(policy, dict) or \
                    set(policy) - {'purpose', 'profile', 'check_ids', 'payload_modes'}:
                raise ValueError('invalid environment handler policy')
            purpose, profile = policy.get('purpose'), policy.get('profile')
            if purpose not in ('development', 'task_e2e', 'full_e2e', 'publishing', 'release'):
                raise ValueError(f'invalid environment purpose for {handler}')
            try:
                name(profile, 'environment profile')
            except WorkloadError as error:
                raise ValueError(f'invalid environment profile for {handler}') from error
            checks = policy.get('check_ids')
            modes = policy.get('payload_modes', [])
            if ((checks is not None and (not isinstance(checks, list) or not checks or len(checks) > 256 or
                    any(not isinstance(item, str) or not item or len(item) > 160 for item in checks) or
                    len(set(checks)) != len(checks))) or
                    not isinstance(modes, list) or len(modes) > 32 or
                    any(not isinstance(item, str) or not item or len(item) > 64 for item in modes) or
                    len(set(modes)) != len(modes) or (modes and purpose != 'development')):
                raise ValueError(f'invalid task check policy for {handler}')
            self.environment_handlers[handler] = dict(purpose=purpose, profile=profile,
                check_ids=tuple(checks) if checks is not None else None, payload_modes=tuple(modes))
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
            if "environment_handle" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN environment_handle TEXT")
            if "environment_generation" not in {row[1] for row in db.execute("PRAGMA table_info(attempts)")}:
                db.execute("ALTER TABLE attempts ADD COLUMN environment_generation INTEGER")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_environment ON jobs(environment_handle,state)")
            environment_columns = {row[1] for row in db.execute("PRAGMA table_info(task_environments)")}
            for column, declaration in (('writer_job', 'TEXT'), ('writer_attempt', 'TEXT'),
                                        ('affinity_started', 'REAL')):
                if column not in environment_columns:
                    db.execute(f'ALTER TABLE task_environments ADD COLUMN {column} {declaration}')

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
        if not result.get('environment_handle'):
            result.pop('environment_handle', None)
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
            "SELECT id,worker,boot,host,fence,state,expires,compilation,environment_generation "
            "FROM attempts WHERE job=? ORDER BY fence",
            (job_id,))]
        for attempt in result['attempts']:
            attempt['compilation'] = json.loads(attempt['compilation']) if attempt['compilation'] else None
        remote = db.execute("SELECT provider,correlation,state,run_id,run_attempt,run_status,reason "
                            "FROM github_remote_jobs WHERE job=?", (job_id,)).fetchone()
        if remote:
            result['remote_execution'] = dict(remote)
        if result.get('environment_handle'):
            env = db.execute('SELECT handle,purpose,profile,state,generation,last_outcome,bytes_used '
                             'FROM task_environments WHERE handle=?', (result['environment_handle'],)).fetchone()
            if env:
                result['environment'] = dict(env)
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

    @property
    def environment_retention_enabled(self):
        return bool(self.limits.environment_idle_seconds and self.limits.environment_generation_seconds)

    def capabilities(self, owner, allowed_handlers):
        """Small authenticated capability response; unsupported is never inferred."""
        allowed = set(allowed_handlers)
        enrolled = sorted(handler for handler, policy in self.environment_handlers.items()
                          if handler in allowed and policy['purpose'] in ('development', 'task_e2e')
                          and self.environment_retention_enabled)
        forbidden = sorted(handler for handler, policy in self.environment_handlers.items()
                           if handler in allowed and policy['purpose'] in ('full_e2e', 'publishing', 'release'))
        result = dict(versions=[1, 2, 3], environments=dict(version=1, handlers=enrolled,
                                                             forbidden_handlers=forbidden))
        encode(result, 8192)
        return result

    def _environment_policy(self, spec):
        reference = spec.get('environment')
        if reference is None:
            return None
        policy = self.environment_handlers.get(spec['handler'])
        if policy is None:
            raise WorkloadError('environment_unsupported: handler is not enrolled', 409)
        if policy['purpose'] in ('full_e2e', 'publishing', 'release'):
            raise WorkloadError('environment_scope_forbidden', 403)
        if policy['purpose'] not in ('development', 'task_e2e'):
            raise WorkloadError('environment_unsupported: handler purpose is not eligible', 409)
        if not self.environment_retention_enabled:
            raise WorkloadError('environment_retention_disabled', 503)
        if policy['purpose'] == 'task_e2e':
            selected = spec['payload'].get('check_ids')
            if (not isinstance(selected, list) or not selected or len(selected) > 64 or
                    any(not isinstance(item, str) or not re.fullmatch(
                        r'[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_-]*', item) for item in selected) or
                    len(set(selected)) != len(selected)):
                raise WorkloadError('environment_scope_forbidden: task E2E requires bounded exact check IDs', 403)
            known = policy.get('check_ids')
            if known is not None and (not set(selected).issubset(known) or set(selected) == set(known)):
                raise WorkloadError('environment_scope_forbidden: task E2E selection is outside its installed allowlist', 403)
        allowed_modes = policy.get('payload_modes', ())
        if allowed_modes and spec['payload'].get('mode') not in allowed_modes:
            raise WorkloadError('environment_scope_forbidden: payload mode is not enrolled for reuse', 403)
        return policy

    def validate_submission(self, owner, request, *, allowed_handlers=None):
        """Validate schema and installed-purpose policy before callers upload source."""
        name(owner, 'owner')
        spec = submission(request, self.handlers if allowed_handlers is None else
                          self.handlers.intersection(allowed_handlers), self.limits)
        self._environment_policy(spec)
        return spec

    def _environment_row(self, db, handle, principal=None, *, delegated_owner=None):
        row = db.execute('SELECT * FROM task_environments WHERE handle=?', (handle,)).fetchone()
        if (row is None or principal is not None and row['principal'] != principal or
                delegated_owner is not None and row['delegated_owner'] != delegated_owner):
            raise WorkloadError('environment not found', 404)
        if (row['idle_expires'] <= self.clock() or row['generation_expires'] <= self.clock()) and \
                not db.execute("SELECT 1 FROM jobs WHERE environment_handle=? AND state IN ('queued','running') LIMIT 1",
                               (handle,)).fetchone():
            raise WorkloadError('environment not found', 404)
        return row

    def _environment_view(self, db, row):
        result = {key: row[key] for key in ('handle', 'purpose', 'profile', 'state', 'generation',
            'last_job', 'last_input_digest', 'last_outcome', 'bytes_used', 'created', 'updated',
            'last_used', 'idle_expires', 'generation_expires')}
        result['replicas'] = [dict(replica) for replica in db.execute(
            'SELECT host,profile,compatibility,generation,state,bytes_used,last_used,seen '
            'FROM task_environment_replicas WHERE handle=? ORDER BY last_used DESC,host LIMIT 2',
            (row['handle'],))]
        encoded = encode(result, 16*1024)
        return json.loads(encoded)

    def get_environment(self, principal, handle):
        if not isinstance(handle, str) or not __import__('re').fullmatch(r'[a-f0-9]{32}', handle):
            raise WorkloadError('invalid environment handle')
        with self.transaction() as db:
            now = self.clock()
            self._expire(db, now)
            self._expire_environments(db, now)
            row = self._environment_row(db, handle, principal)
            return self._environment_view(db, row)

    def _expire_environments(self, db, now):
        """Delete only a bounded batch; unset/zero retention never deletes."""
        stale = db.execute("SELECT e.handle,e.writer_attempt,e.generation,e.profile,e.writer_job,e.updated,"
                           "a.id AS attempt_id,a.host,a.environment_generation,a.state AS attempt_state,w.seen "
                           "FROM task_environments e LEFT JOIN attempts a ON a.id=e.writer_attempt "
                           "LEFT JOIN workers w ON w.id=a.worker "
                           "WHERE e.writer_attempt IS NOT NULL ORDER BY e.updated,e.handle LIMIT ?",
                           (self.limits.environment_sweep_rows,)).fetchall()
        stale_writers, stale_replicas = [], []
        for row in stale:
            ended = row['attempt_state'] in (None, 'ended')
            abandoned = row['attempt_state'] == 'cleanup' and (
                row['seen'] is None or row['seen'] <= now-self.limits.cleanup_seconds)
            if ended or abandoned:
                stale_writers.append((row['handle'], row['writer_attempt'], row['generation']))
                if row['host']:
                    stale_replicas.append((row['handle'], row['host']))
        if stale_writers:
            values = ','.join('('+','.join('?' for _ in range(3))+')' for _ in stale_writers)
            db.execute("UPDATE task_environments SET state='rebuild_required',compatibility=NULL,"
                       'writer_job=NULL,writer_attempt=NULL,affinity_started=NULL,updated=? '
                       f'WHERE (handle,writer_attempt,generation) IN (VALUES {values})',
                       (now, *(value for row in stale_writers for value in row)))
        if stale_replicas:
            values = ','.join('('+','.join('?' for _ in range(2))+')' for _ in stale_replicas)
            db.execute("UPDATE task_environment_replicas SET state='rebuild_required',compatibility=NULL,seen=? "
                       f'WHERE (handle,host) IN (VALUES {values})',
                       (now, *(value for row in stale_replicas for value in row)))
        idle, absolute = self.limits.environment_idle_seconds, self.limits.environment_generation_seconds
        if not idle or not absolute:
            return 0
        rows = db.execute('SELECT handle FROM task_environments WHERE idle_expires<=? OR generation_expires<=? '
                          'ORDER BY min(idle_expires,generation_expires),handle LIMIT ?',
                          (now, now, self.limits.environment_sweep_rows)).fetchall()
        if rows:
            handles = [row['handle'] for row in rows]
            placeholders = ','.join('?' for _ in handles)
            db.execute('DELETE FROM task_environments WHERE handle IN ('+placeholders+') AND NOT EXISTS '
                       '(SELECT 1 FROM jobs WHERE jobs.environment_handle=task_environments.handle '
                       "AND jobs.state IN ('queued','running')) AND writer_attempt IS NULL AND NOT EXISTS "
                       "(SELECT 1 FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.environment_handle=? "
                       "AND a.state IN ('running','cleanup'))", (*handles, *handles))
        return len(rows)

    def _resolve_environment(self, db, owner, spec, now, job_id):
        reference = spec.get('environment')
        if reference is None:
            return None
        policy = self._environment_policy(spec)
        delegated_owner = spec.get('labels', {}).get('owner', '')
        if 'handle' in reference:
            row = self._environment_row(db, reference['handle'], owner, delegated_owner=delegated_owner)
            if row['profile'] != policy['profile'] or row['purpose'] != policy['purpose']:
                raise WorkloadError('environment_profile_mismatch', 409)
            handle = row['handle']
        else:
            environment_key = reference['key']
            row = db.execute('SELECT * FROM task_environments WHERE principal=? AND delegated_owner=? '
                             'AND environment_key=?', (owner, delegated_owner, environment_key)).fetchone()
            if row and (row['idle_expires'] <= now or row['generation_expires'] <= now) and not db.execute(
                    "SELECT 1 FROM jobs WHERE environment_handle=? AND state IN ('queued','running') LIMIT 1",
                    (row['handle'],)).fetchone():
                db.execute('DELETE FROM task_environments WHERE handle=?', (row['handle'],))
                row = None
            if row is None:
                count = db.execute('SELECT count(*) FROM task_environments').fetchone()[0]
                owner_count = db.execute('SELECT count(*) FROM task_environments WHERE principal=? AND delegated_owner=?',
                                         (owner, delegated_owner)).fetchone()[0]
                if count >= self.limits.environment_registry or owner_count >= self.limits.environments_per_owner:
                    raise WorkloadError('environment_registry_capacity', 429)
                idle, absolute = self.limits.environment_idle_seconds, self.limits.environment_generation_seconds
                handle = uuid.uuid4().hex
                encode(dict(principal=owner, delegated_owner=delegated_owner, key=environment_key,
                            purpose=policy['purpose'], profile=policy['profile']), 4096)
                db.execute('INSERT INTO task_environments(handle,principal,delegated_owner,environment_key,purpose,profile,'
                           'state,generation,last_job,last_input_digest,replicas,bytes_used,created,updated,last_used,'
                           'idle_expires,generation_expires) VALUES(?,?,?,?,?,?,'
                           "'empty',0,?,?, '[]',0,?,?,?,?,?)",
                           (handle, owner, delegated_owner, environment_key, policy['purpose'], policy['profile'],
                            job_id, spec['input_digest'], now, now, now, now+idle, now+absolute))
            else:
                if row['profile'] != policy['profile'] or row['purpose'] != policy['purpose']:
                    raise WorkloadError('environment_profile_mismatch', 409)
                handle = row['handle']
        db.execute('UPDATE task_environments SET last_job=?,last_input_digest=?,updated=?,last_used=?,idle_expires=? '
                   'WHERE handle=?', (job_id, spec['input_digest'], now, now,
                                      now+self.limits.environment_idle_seconds, handle))
        return self._environment_row(db, handle, owner, delegated_owner=delegated_owner)

    def submit(self, owner, request, *, allowed_handlers=None):
        spec = self.validate_submission(owner, request, allowed_handlers=allowed_handlers)
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
            self._expire_environments(db, now)
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
            env = self._resolve_environment(db, owner, spec, now, jid)
            if env is not None:
                db.execute('UPDATE jobs SET environment_handle=? WHERE id=?', (env['handle'], jid))
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
        if not isinstance(report, dict) or set(report) - {"capacity", "available", "labels", "handlers", "ready", "host",
                                                          "environment_profiles", "environment_replicas"}:
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
        profiles = report.get('environment_profiles', {})
        if not isinstance(profiles, dict) or len(profiles) > 32:
            raise WorkloadError('invalid environment profile report')
        body['environment_profiles'] = {}
        for profile, compatibility in profiles.items():
            name(profile, 'environment profile')
            if not isinstance(compatibility, str) or not re.fullmatch(r'[a-f0-9]{64}', compatibility):
                raise WorkloadError('invalid environment profile compatibility')
            body['environment_profiles'][profile] = compatibility
        replicas = report.get('environment_replicas', [])
        if not isinstance(replicas, list) or len(replicas) > 64:
            raise WorkloadError('invalid environment replica report')
        body['environment_replicas'] = []
        seen_handles = set()
        for replica in replicas:
            if not isinstance(replica, dict) or set(replica) != {
                    'handle', 'profile', 'compatibility', 'generation', 'state', 'bytes_used', 'last_used'}:
                raise WorkloadError('invalid environment replica report')
            handle, profile = replica['handle'], replica['profile']
            if not isinstance(handle, str) or not re.fullmatch(r'[a-f0-9]{32}', handle) or handle in seen_handles:
                raise WorkloadError('invalid or duplicate environment replica handle')
            name(profile, 'environment profile')
            compatibility = replica['compatibility']
            if compatibility is not None and (not isinstance(compatibility, str) or
                    not re.fullmatch(r'[a-f0-9]{64}', compatibility)):
                raise WorkloadError('invalid environment replica compatibility')
            generation, used, last_used = replica['generation'], replica['bytes_used'], replica['last_used']
            if (isinstance(generation, bool) or not isinstance(generation, int) or generation < 0 or
                    isinstance(used, bool) or not isinstance(used, int) or not 0 <= used <= 32*1024**3 or
                    isinstance(last_used, bool) or not isinstance(last_used, (int, float)) or
                    not math.isfinite(last_used) or last_used < 0 or replica['state'] not in ('parked', 'rebuild_required')):
                raise WorkloadError('invalid environment replica measurements')
            seen_handles.add(handle)
            body['environment_replicas'].append(dict(handle=handle, profile=profile,
                compatibility=compatibility, generation=generation, state=replica['state'],
                bytes_used=used, last_used=last_used))
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
            environment_cleanup = []
            if 'environment_replicas' in report:
                reported = body['environment_replicas']
                reported_handles = {item['handle'] for item in reported}
                if reported_handles:
                    placeholders = ','.join('?' for _ in reported_handles)
                    db.execute(f"DELETE FROM task_environment_replicas WHERE host=? AND handle NOT IN ({placeholders})",
                               (host_id, *sorted(reported_handles)))
                    handles = sorted(reported_handles)
                    environment_rows = db.execute(
                        f"SELECT e.handle,e.generation,e.writer_attempt,count(r.host) AS other_hosts "
                        f"FROM task_environments e LEFT JOIN task_environment_replicas r "
                        f"ON r.handle=e.handle AND r.host!=? WHERE e.handle IN ({placeholders}) "
                        "GROUP BY e.handle,e.generation,e.writer_attempt",
                        (host_id, *handles)).fetchall()
                    environments = {row['handle']: row for row in environment_rows}
                    existing_rows = db.execute(
                        f"SELECT handle FROM task_environment_replicas WHERE host=? AND handle IN ({placeholders})",
                        (host_id, *handles)).fetchall()
                    existing_handles = {row['handle'] for row in existing_rows}
                    host_rows = db.execute('SELECT count(*) FROM task_environment_replicas WHERE host=?',
                                            (host_id,)).fetchone()[0]
                else:
                    db.execute("DELETE FROM task_environment_replicas WHERE host=?", (host_id,))
                    environments, existing_handles, host_rows = {}, set(), 0
                candidates = []
                for replica in reported:
                    handle = replica['handle']
                    environment = environments.get(handle)
                    if environment is None:
                        environment_cleanup.append(dict(handle=handle, generation=replica['generation']))
                        continue  # stale disk state is never a scheduler cache hit
                    if replica['generation'] != environment['generation']:
                        environment_cleanup.append(dict(handle=handle, generation=replica['generation']))
                        continue  # returning hosts must reclaim superseded local bytes
                    if (replica['state'] != 'parked' or environment['writer_attempt'] is not None):
                        continue  # uncommitted and fenced generations are not cache hits
                    if environment['other_hosts'] >= 2:
                        raise WorkloadError('environment replica registry capacity exhausted', 429)
                    candidates.append((handle, host_id, replica['profile'], replica['compatibility'],
                                       replica['generation'], replica['state'], replica['bytes_used'],
                                       replica['last_used'], now))
                candidate_handles = {row[0] for row in candidates}
                stale_handles = existing_handles - candidate_handles
                if stale_handles:
                    stale_sql = ','.join('?' for _ in stale_handles)
                    db.execute(f'DELETE FROM task_environment_replicas WHERE host=? AND handle IN ({stale_sql})',
                               (host_id, *sorted(stale_handles)))
                new_rows = sum(1 for row in candidates if row[0] not in existing_handles)
                if host_rows - len(stale_handles) + new_rows > 64:
                    raise WorkloadError('environment replica registry capacity exhausted', 429)
                if candidates:
                    values = ','.join('('+','.join('?' for _ in range(9))+')' for _ in candidates)
                    db.execute('INSERT INTO task_environment_replicas(handle,host,profile,compatibility,generation,state,'
                               'bytes_used,last_used,seen) VALUES '+values+
                               ' ON CONFLICT(handle,host) DO UPDATE SET profile=excluded.profile,'
                               'compatibility=excluded.compatibility,generation=excluded.generation,'
                               'state=excluded.state,bytes_used=excluded.bytes_used,last_used=excluded.last_used,'
                               'seen=excluded.seen', tuple(value for row in candidates for value in row))
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
                self._release_environment_writer(db, row, now)
            dirty = db.execute("SELECT count(*) FROM attempts WHERE worker=? AND state='cleanup'", (worker_id,)).fetchone()[0]
            ready = bool(body["ready"] and not dirty)
            db.execute("UPDATE workers SET ready=? WHERE id=?", (ready, worker_id))
            return {"worker": worker_id, "boot": boot, "ready": ready,
                    "cleanup": [r[0] for r in db.execute("SELECT id FROM attempts WHERE worker=? AND state='cleanup'", (worker_id,))],
                    "environment_cleanup": environment_cleanup}

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
            self._expire_environments(db, now)
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
        result = {"job_id": job["id"], "attempt_id": attempt["id"], "fence": attempt["fence"],
                 "lease_remaining": max(0, attempt["expires"] - self.clock()),
                 "expires": attempt["expires"], "worker": attempt["worker"], "boot": attempt["boot"],
                 "owner": job["owner"], "spec": job["spec"],
                 "compilation": json.loads(attempt['compilation']) if attempt['compilation'] else None}
        if job.get('environment_handle'):
            env = db.execute('SELECT * FROM task_environments WHERE handle=?',
                             (job['environment_handle'],)).fetchone()
            owner_scope = hashlib.sha256((job['owner'] + '\0' + env['delegated_owner']).encode()).hexdigest()
            result['environment'] = dict(handle=env['handle'], purpose=env['purpose'], profile=env['profile'],
                generation=attempt['environment_generation'], compatibility=env['compatibility'],
                owner_scope=owner_scope, queue_seconds=max(0, self.clock()-job['created']),
                replicas=[dict(replica) for replica in db.execute(
                    'SELECT host,profile,compatibility,generation,state,bytes_used,last_used,seen '
                    'FROM task_environment_replicas WHERE handle=? ORDER BY last_used DESC,host LIMIT 2',
                    (env['handle'],))])
        return result

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

    def complete(self, worker, boot, attempt_id, fence, *, input_digest, outcome, result,
                  environment_receipt=None):
        if outcome not in ("succeeded", "product_failure", "infrastructure"):
            raise WorkloadError("invalid outcome")
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
            completion = {"outcome": outcome, "input_digest": input_digest, "result": result}
            env_handle = job.get('environment_handle')
            env = None
            if env_handle:
                if a['environment_generation'] is None:
                    raise WorkloadError('environment_generation_missing', 409)
                env = db.execute('SELECT * FROM task_environments WHERE handle=?', (env_handle,)).fetchone()
                if env is None:
                    raise WorkloadError('environment_writer_fenced', 409)
                receipt = validate_environment_receipt(
                    environment_receipt, handle=env_handle, generation=a['environment_generation'],
                    profile=env['profile'], source_digest=input_digest)
                completion['environment_receipt'] = receipt
            elif environment_receipt is not None:
                raise WorkloadError('unexpected_environment_receipt', 400)
            raw = encode(completion, self.limits.record_bytes)
            if a["state"] == "ended" and a["result"] == raw:
                return job  # Idempotent acknowledgement, including infrastructure retry.
            if a["state"] != "running" or job["fence"] != fence or job["state"] != "running":
                raise WorkloadError("attempt has been fenced", 409)
            if env is not None and (env['writer_job'] != job['id'] or env['writer_attempt'] != attempt_id or
                                    env['generation'] != a['environment_generation']):
                raise WorkloadError('environment_writer_fenced', 409)
            from .artifacts import validate_artifacts
            validate_artifacts(db, job['owner'], result)
            # complete is sent only AFTER owned processes/containers are stopped.
            db.execute("UPDATE attempts SET state='ended',result=? WHERE id=?", (raw, attempt_id))
            if env is not None:
                parked = receipt['state'] == 'parked'
                db.execute('UPDATE task_environments SET state=?,compatibility=?,last_outcome=?,last_job=?,'
                           'last_input_digest=?,bytes_used=?,last_used=?,updated=?,idle_expires=?,'
                           'writer_job=NULL,writer_attempt=NULL WHERE handle=? AND writer_attempt=? AND generation=?',
                           (receipt['state'], receipt['compatibility'] if parked else None,
                            receipt['reuse_outcome'], job['id'], input_digest, receipt['bytes_used'], now, now,
                            now+self.limits.environment_idle_seconds, env_handle, attempt_id,
                            a['environment_generation']))
                if parked:
                    db.execute('INSERT INTO task_environment_replicas(handle,host,profile,compatibility,generation,state,'
                               'bytes_used,last_used,seen) VALUES(?,?,?,?,?,'\
                               "'parked',?,?,?) ON CONFLICT(handle,host) DO UPDATE SET "
                               'profile=excluded.profile,compatibility=excluded.compatibility,'
                               'generation=excluded.generation,state=excluded.state,bytes_used=excluded.bytes_used,'
                               'last_used=excluded.last_used,seen=excluded.seen',
                               (env_handle, a['host'], env['profile'], receipt['compatibility'],
                                a['environment_generation'], receipt['bytes_used'], now, now))
                else:
                    db.execute("UPDATE task_environment_replicas SET state='rebuild_required',compatibility=NULL,seen=? "
                               'WHERE handle=? AND host=?', (now, env_handle, a['host']))
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

    def _release_environment_writer(self, db, attempt, now):
        """Release a retained replica only after the worker proves cleanup."""
        job = db.execute('SELECT environment_handle FROM jobs WHERE id=?', (attempt['job'],)).fetchone()
        handle = job['environment_handle'] if job else None
        if not handle or attempt['environment_generation'] is None:
            return
        changed = db.execute("UPDATE task_environments SET state='rebuild_required',compatibility=NULL,"
                             'writer_job=NULL,writer_attempt=NULL,updated=?,last_used=?,affinity_started=NULL '
                             'WHERE handle=? AND writer_attempt=? AND generation=?',
                             (now, now, handle, attempt['id'], attempt['environment_generation'])).rowcount
        if changed:
            db.execute("UPDATE task_environment_replicas SET state='rebuild_required',compatibility=NULL,seen=? "
                       'WHERE handle=? AND host=?', (now, handle, attempt['host']))

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
            now = self.clock()
            self._expire(db, now)
            self._expire_environments(db, now)
            self._prune(db, now)
            db.execute("UPDATE github_remote_jobs SET state='terminal',updated=? WHERE state IN ('running','cancel_requested') "
                       "AND run_status='completed' AND NOT EXISTS "
                       "(SELECT 1 FROM attempts a WHERE a.job=github_remote_jobs.job AND a.state IN ('running','cleanup'))",
                       (self.clock(),))
        with closing(self.connect()) as db:
            db.execute("PRAGMA wal_checkpoint(PASSIVE)")
