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
 progress TEXT, result TEXT, environment_generation INTEGER,
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
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, created);
CREATE INDEX IF NOT EXISTS attempts_host ON attempts(host, state);
CREATE INDEX IF NOT EXISTS github_remote_state ON github_remote_jobs(provider, state, created);
