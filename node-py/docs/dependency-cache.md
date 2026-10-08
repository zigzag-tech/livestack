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

## Opt-in per handler (worker-owned)

Only handlers whose entry in worker.json `handlers` says `"dependency_cache": true` receive restores or stores;
every other handler (compilation, release, ...) never gets a tree copied in, even when its source carries a
manifest. The boolean `true` is the only opt-in: absent, `false`, a string or a number is not opted in, and a
non-boolean value is logged (`dependency_cache not used for <handler>: invalid: ...`). The flag is deliberately
not read from the job's source or the handler package. For registry-managed handlers the entry is the worker's
base handler (`handler_config` starts from it), so no package release is needed to change it; restart the worker
(idle) to apply a config change.

## Handler contract (handshake)

The worker cannot restore before the handler starts: a job's source is often not the handler's working tree
until the handler has materialised it (Benchday's ZZOPS archive is `app/` plus sibling dependency groups
that the handler merges into one tree, and any extra file in the raw layout is refused). So the handler asks:

1. `HARMONY_DEPENDENCY_CACHE_HANDSHAKE` names a directory. Absent = no cache; install as usual.
2. When its tree is ready, the handler writes `<dir>/request.json` `{"version": 1, "root": "<absolute tree root>"}`
   (atomic rename). `root` must lie inside the attempt's source directory and contain `.livestack/dependency-cache.json`.
3. The worker (polling every 0.2 s while the attempt runs) restores what matches and writes `<dir>/response.json`
   `{"version": 1, "outcome": "restored"|"skipped"|"error", "components": [{"path": "hub/node_modules", "outcome": "reused"}]}`
   once. The handler waits for it (bounded) and does not install the trees named `reused`. On timeout it installs
   everything (cold).
4. After every install succeeded, the handler writes `$HARMONY_OUTPUT/dependency-cache-commit.json`:
   `{"version": 1, "paths": ["hub/node_modules", ...]}`. Never for trees later pruned or mutated. The worker stores
   the committed trees that missed, reading them from the `root` the handler gave.

## Worker config (worker.json, no environment variables)

    "dependency_cache": {"enabled": true, "path": "/home/ubuntu/.cache/livestack-dependency-cache/<worker>",
                         "max_bytes": 8589934592, "epoch": 0, "refresh_every": 20, "max_component_bytes": 4294967296}

`path` absolute and private (0700, worker uid), its own per worker. `epoch` bump = everything misses.
Missing, `enabled:false` or invalid = disabled (invalid is logged by name). Linux only, not task environments.

## Key, safety, bound

Key = sha256 of (schema, epoch, owner namespace, handler executable realpath/size/mtime + OS/arch/libc, tree path,
key file contents). Namespace is the submitting principal (`assignment['owner']`), as in `docker_cache`.
A tree whose parent directory is an alias symlink (Benchday's `packages/mesh_relay`) is restored and stored at the
parent's real location, provided that stays inside the source (npm installed it there); otherwise `parent-outside-source`.
Restores of the trees of one attempt run concurrently (8 threads): two trees (hub 640 MB, jingway-framework) carry
most bytes and the rest are under a second, so wall time is the slowest copy, not the sum.
Restore is a copy (`cp -a --reflink=auto`) then a re-scan compared with the entry's recorded file count and apparent (`st_size`) bytes; allocation differs between a tree and its copy and is only used for the bound;
a mismatch drops the entry and the attempt installs cold. Store refuses absolute/escaping symlinks, special files,
unreadable subtrees, trees over `max_component_bytes` or `max_bytes/2`, and a key that changed during the attempt.
`refresh_every` N: every Nth attempt (sha256 of attempt id mod N) skips restore, installs cold and replaces the
entry, bounding undetected same-size corruption. Bound: `max_bytes` per namespace, LRU by `meta.json` mtime,
enforced at every store. Layout: `<path>/<ns>/entries/<key>/{data,meta.json}`, `<ns>/.lock` (flock: shared restore,
exclusive store; busy = named miss).

## Outcome (never silent)

Attempt result `dependency_cache`: `{outcome: enabled, restored: [{path, outcome, reason?, key, bytes, seconds}],
saved: [...]}`. Restore outcomes: `reused`, `miss` (`no-entry`, `busy`, `verify-failed: ...`, `copy-failed`),
`refresh`, `skipped` (`no-manifest`, `invalid-manifest: ...`, `no-key`, `destination-exists`, `parent-outside-source`,
`unsupported-pattern`). Save outcomes: `saved`, `not-saved` (reason).

## Operations / rollback

Purge: delete `<path>/<ns>` (files are owned by the worker uid) or bump `epoch`. Roll back: remove the
`dependency_cache` block (or `enabled:false`) and restart the worker when idle. Roll out per
`worker-release-rollout.md`; the config key survives a release roll.
