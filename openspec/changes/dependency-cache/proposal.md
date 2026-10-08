## Why

Every Benchday isolated-E2E attempt re-runs `npm ci` in 16 workspaces (about 68 s of a
250 s attempt, measured 2026-10-08) even though no lockfile changed. The docker build
cache (`docker-build-cache`) made the image free; the installed dependency trees are the
largest remaining repeat work, and any app with a lockfile has the same shape. The
mechanism is generic (a keyed, bounded, per-principal tree store); only the declaration
of what to key and where to restore is the app's.

## What Changes

- Worker config `dependency_cache: {enabled, path, max_bytes, epoch, refresh_every,
  max_component_bytes}` (strictly validated, no environment variables). Missing,
  disabled or invalid means today's behaviour exactly.
- The job's source declares components in `.livestack/dependency-cache.json`
  (`{version, components:[{path, for_each?, key_paths}]}`, closed schema, `{}` expands
  over `for_each` directories so workspaces are discovered, not enumerated).
- Before the handler starts, for each concrete tree whose key (hash of the `key_paths`
  contents, owner namespace, epoch, handler executable identity, platform/arch/libc) has
  an entry, the worker copies it into the source and names it in
  `HARMONY_DEPENDENCY_CACHE_COMPONENTS`.
- The handler writes `dependency-cache-commit.json` (`{version, paths}`) into its output
  once the trees are complete; the worker stores committed trees that missed.
- Bounded: `max_bytes` per principal namespace with LRU eviction at store time; a tree
  larger than `max_component_bytes` (or half `max_bytes`) is never stored.
- Fail closed to a cold install, loudly: every unusable condition is a named outcome in
  the attempt result `dependency_cache`; a fault never fails a job.

## Impact

New module `dependency_cache.py` (sibling of `docker_cache.py`, which is not edited),
about 40 lines of wiring in `worker.py`. Handlers opt in by reading the environment
variable and writing the commit file; handlers that ignore both are unaffected.
