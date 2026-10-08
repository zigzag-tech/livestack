# Persistent dependency cache

Status: implemented 2026-10-08 (openspec `dependency-cache`). Code: `livestack_node/workloads/dependency_cache.py`,
wired in `worker.py`. Sibling of the docker build cache (`docker-build-cache.md`); the two are independent.

## What it does

Skips repeat installs (`npm ci` and similar) for jobs whose lockfile did not change. The app declares what to key
and where to restore; the worker keeps the trees, bounded and per principal.

## Declaring (in the job's source)

`.livestack/dependency-cache.json`, closed schema, at most 16 components, 64 concrete trees:

    {"version": 1, "components": [
      {"path": "{}/node_modules",
       "for_each": ["hub", "packages/*"],
       "key_paths": ["{}/package.json", "{}/package-lock.json"]}]}

`{}` is replaced by every directory `for_each` matches (`*` only in the last segment; directories that do not
exist are skipped). A component without `for_each` names one literal path and no `{}`. `key_paths` are files or
directories (hashed recursively); all must exist or the tree is not cached (`no-key`).

## Handler contract

1. `HARMONY_DEPENDENCY_CACHE_COMPONENTS` is a JSON list `[{"path": "hub/node_modules", "outcome": "reused"}]` of
   trees already restored: do not install them. Absent variable = no cache.
2. After every install succeeded, write `$HARMONY_OUTPUT/dependency-cache-commit.json`:
   `{"version": 1, "paths": ["hub/node_modules", ...]}`. Never write it for trees later pruned or mutated.

## Worker config (worker.json, no environment variables)

    "dependency_cache": {"enabled": true, "path": "/home/ubuntu/.cache/livestack-dependency-cache/<worker>",
                         "max_bytes": 8589934592, "epoch": 0, "refresh_every": 20, "max_component_bytes": 4294967296}

`path` absolute and private (0700, worker uid), its own per worker. `epoch` bump = everything misses.
Missing, `enabled:false` or invalid = disabled (invalid is logged by name). Linux only, not task environments.

## Key, safety, bound

Key = sha256 of (schema, epoch, owner namespace, handler executable realpath/size/mtime + OS/arch/libc, tree path,
key file contents). Namespace is the submitting principal (`assignment['owner']`), as in `docker_cache`.
Restore is a copy (`cp -a --reflink=auto`) then a re-scan compared with the entry's recorded file count and bytes;
a mismatch drops the entry and the attempt installs cold. Store refuses absolute/escaping symlinks, special files,
unreadable subtrees, trees over `max_component_bytes` or `max_bytes/2`, and a key that changed during the attempt.
`refresh_every` N: every Nth attempt (sha256 of attempt id mod N) skips restore, installs cold and replaces the
entry, bounding undetected same-size corruption. Bound: `max_bytes` per namespace, LRU by `meta.json` mtime,
enforced at every store. Layout: `<path>/<ns>/entries/<key>/{data,meta.json}`, `<ns>/.lock` (flock: shared restore,
exclusive store; busy = named miss).

## Outcome (never silent)

Attempt result `dependency_cache`: `{outcome: enabled, restored: [{path, outcome, reason?, key, bytes, seconds}],
saved: [...]}`. Restore outcomes: `reused`, `miss` (`no-entry`, `busy`, `verify-failed: ...`, `copy-failed`),
`refresh`, `skipped` (`no-manifest`, `invalid-manifest: ...`, `no-key`, `destination-exists`, `symlinked-parent`,
`unsupported-pattern`). Save outcomes: `saved`, `not-saved` (reason).

## Operations / rollback

Purge: delete `<path>/<ns>` (files are owned by the worker uid) or bump `epoch`. Roll back: remove the
`dependency_cache` block (or `enabled:false`) and restart the worker when idle. Roll out per
`worker-release-rollout.md`; the config key survives a release roll.
