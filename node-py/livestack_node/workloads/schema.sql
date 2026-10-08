PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, spec TEXT NOT NULL, state TEXT NOT NULL,
 fence INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
 labels TEXT NOT NULL DEFAULT '{}', reason TEXT, result TEXT, retain INTEGER NOT NULL DEFAULT 0,
 environment_handle TEXT,
 UNIQUE(owner, request_key)
);
CREATE TABLE IF NOT EXISTS workers (
 id TEXT PRIMARY KEY, host TEXT NOT NULL, boot TEXT NOT NULL,
 report TEXT NOT NULL, seen REAL NOT NULL, ready INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempts (
 id TEXT PRIMARY KEY, job TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 worker TEXT NOT NULL REFERENCES workers(id), boot TEXT NOT NULL,
 host TEXT NOT NULL, fence INTEGER NOT NULL, state TEXT NOT NULL,
 need TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL,
 progress TEXT, result TEXT, environment_generation INTEGER, handler_release TEXT,
 decision_id TEXT,
 UNIQUE(job, fence)
);
CREATE TABLE IF NOT EXISTS task_environments (
 handle TEXT PRIMARY KEY,
 principal TEXT NOT NULL,
 delegated_owner TEXT NOT NULL,
 environment_key TEXT NOT NULL,
 purpose TEXT NOT NULL,
 profile TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'empty',
 generation INTEGER NOT NULL DEFAULT 0,
 last_job TEXT,
 last_input_digest TEXT,
 compatibility TEXT,
 last_outcome TEXT,
 writer_job TEXT,
 writer_attempt TEXT,
 affinity_started REAL,
 replicas TEXT NOT NULL DEFAULT '[]',
 bytes_used INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL,
 updated REAL NOT NULL,
 last_used REAL NOT NULL,
 idle_expires REAL NOT NULL,
 generation_expires REAL NOT NULL,
 UNIQUE(principal, delegated_owner, environment_key)
);
CREATE INDEX IF NOT EXISTS task_environments_owner ON task_environments(principal, delegated_owner, last_used);
CREATE INDEX IF NOT EXISTS task_environments_expiry ON task_environments(idle_expires, generation_expires);
CREATE TABLE IF NOT EXISTS task_environment_replicas (
 handle TEXT NOT NULL,
 host TEXT NOT NULL,
 profile TEXT NOT NULL,
 compatibility TEXT,
 generation INTEGER NOT NULL,
 state TEXT NOT NULL,
 bytes_used INTEGER NOT NULL,
 last_used REAL NOT NULL,
 seen REAL NOT NULL,
 PRIMARY KEY(handle,host)
);
CREATE INDEX IF NOT EXISTS task_environment_replicas_host ON task_environment_replicas(host,seen);
CREATE INDEX IF NOT EXISTS task_environment_replicas_handle ON task_environment_replicas(handle,seen);
CREATE TABLE IF NOT EXISTS github_remote_jobs (
 job TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
 provider TEXT NOT NULL, correlation TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL, dispatch_started REAL, run_id TEXT, run_attempt INTEGER,
 run_status TEXT, reason TEXT, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS handler_releases(
 handler_id TEXT NOT NULL, release_digest TEXT NOT NULL, archive_digest TEXT NOT NULL,
 archive_bytes INTEGER NOT NULL, manifest TEXT NOT NULL, actor TEXT NOT NULL,
 created REAL NOT NULL, PRIMARY KEY(handler_id,release_digest), UNIQUE(release_digest));
CREATE TABLE IF NOT EXISTS handler_registry_generations(
 generation INTEGER PRIMARY KEY, defaults TEXT NOT NULL, actor TEXT NOT NULL,
 request_id TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS handler_registry_state(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS handler_activation_receipts(
 request_id TEXT PRIMARY KEY, actor TEXT NOT NULL, expected_generation INTEGER NOT NULL,
 generation INTEGER NOT NULL, handler_id TEXT NOT NULL, previous_digest TEXT,
 release_digest TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS handler_release_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL,
 handler_id TEXT NOT NULL, release_digest TEXT, archive_digest TEXT,
 bytes INTEGER NOT NULL, outcome TEXT NOT NULL, policy_revision TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS handler_release_events_age ON handler_release_events(id);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, created);
CREATE INDEX IF NOT EXISTS attempts_host ON attempts(host, state);
CREATE INDEX IF NOT EXISTS github_remote_state ON github_remote_jobs(provider, state, created);
-- declarative-worker-rollout: operator claims (drain/enable) with owner, expiry and
-- compare-and-swap generation. See claims.py. file_value is the last claim_enabled seen
-- in authority.json, so a later file edit is recognised as a change (migration path).
CREATE TABLE IF NOT EXISTS worker_claims(
  worker TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL,
  generation INTEGER NOT NULL,
  owner TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  expires_at REAL,
  needs_operator INTEGER NOT NULL DEFAULT 0,
  file_value INTEGER,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rollout_state(
  key TEXT PRIMARY KEY,
  generation INTEGER NOT NULL,
  body TEXT NOT NULL,
  updated_at REAL NOT NULL
);
