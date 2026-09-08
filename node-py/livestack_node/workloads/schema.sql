PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, spec TEXT NOT NULL, state TEXT NOT NULL,
 fence INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
 reason TEXT, result TEXT, retain INTEGER NOT NULL DEFAULT 0,
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
 result TEXT, UNIQUE(job, fence)
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, created);
CREATE INDEX IF NOT EXISTS attempts_host ON attempts(host, state);
