## Why

`dependency-cache` was built for `npm ci` trees in test attempts. Benchday's release
builds spend 12-15 of their 14-17 minutes compiling the same few hundred registry crates
on every attempt (the attempt's HOME, and so its CARGO_HOME, is fresh each time), and the
artifacts they would reuse are shipped to users, signed. Reuse is only acceptable if a
poisoned or stale entry cannot change shipped bytes unnoticed. The cache as built checks a
restored tree by file count and size, trusts the handler's say-so for what to store even
when the attempt failed, lets the handler's own sandbox reach the store, and gives the
operator no way to compare a cache-assisted result with a cold one. Those are fine for a
test attempt and not for a release.

Design record: no `_plans/*.md` file covers the dependency cache, so nothing there is stale; the record is `openspec/changes/dependency-cache` (this change extends it; that change's
"Not covered: build outputs" stays true: the cache still never keys on source that changes
every train, the handler decides what is a reusable tree).

## What Changes

- Every entry records a content digest of its tree (kind, path, executable bit, size,
  sha256 of each file, link targets) computed at store time on the source and re-checked on
  the copy; restore recomputes it on the copy it hands to the handler and refuses (drops the
  entry, named miss `verify-failed: digest-mismatch`) any difference. An entry written
  before digests existed is refused (`no-digest`) and dropped.
- The attempt's result and `response.json` carry, per restored tree, the full key and the
  digest it was served under, so a handler can put them in its receipt.
- Only a `succeeded` attempt writes the store (`not-saved: attempt-not-succeeded` otherwise).
- The attempt's sandbox cannot see the cache store (`InaccessiblePaths` on the store path),
  so a build script or test process cannot write it; the worker, outside the sandbox, is the
  only writer.
- `audit_every` N (worker config, default 0): every Nth attempt (hash of attempt id, different
  salt from `refresh_every`) is told in `response.json` (`audit: true`) to also produce its
  result without the restored trees and compare. The comparison is the handler's.
- The commit file may name `replace` paths: a restored tree the handler found wrong and
  rebuilt cold is stored over the bad entry.

## Impact

`dependency_cache.py` (digest, audit flag, replace), `worker.py` (success-only save, store
hidden from the sandbox), `node-py/docs/dependency-cache.md`. Existing opted-in handlers
(none in production on release workers yet; e2e workers use it) lose their pre-digest
entries once and repopulate. Nothing changes for handlers that do not opt in.
