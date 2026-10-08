## Why

A Benchday isolated-E2E attempt spends 16-21 minutes in setup and seconds in
assertions; about 10 minutes of that is the in-attempt `docker build`
(apt, tmux, cargo check/build of five binaries). Every attempt gets a private
rootless dockerd whose data root is `<attempt>/docker-data`, deleted with the
attempt (`docker_runtime.remove_data`). No layer and no `RUN --mount=type=cache`
survives, so each attempt builds cold. A spike that saved/loaded image archives
measured NO-GO (load alone 78 s). The data root itself is what BuildKit's cache
lives in; keeping it between attempts makes layer cache and cache mounts work
natively for any app, with no per-app stage declarations.

Realises: `_plans/durable-workloads.md` (private per-attempt Docker). That record
says the Docker data root is attempt-private and removed; this change makes it
optionally persistent per worker and per principal. It does not touch the
containment model (one dockerd, one cgroup, one attempt at a time per root).

## What Changes

- Worker config `docker_cache: {enabled, path, max_bytes, epoch, canary_every, max_growth}`
  (strictly validated, no environment variables). Missing, disabled or invalid
  means today's behaviour exactly; an invalid block is logged by name.
- `docker_command.py` uses `<path>/<namespace>/slot` as the Docker data root when
  enabled. Namespace is derived from the assignment's submitting principal
  (`assignment['owner']`, never the user-controllable `labels.owner`), so two
  principals never share a root. The root is guarded by an exclusive `flock`
  held by the namespace process for the attempt; a busy lock runs the attempt on
  an ordinary ephemeral root (outcome `cold-locked`).
- `remove_data()` never touches a persistent root (it lives outside the workspace).
- End of attempt, dockerd still up: remove containers/volumes/networks, prune
  BuildKit cache to `max_bytes`, escalate to unused images only when still over;
  after dockerd stops, measure the root and DISCARD it if over `max_bytes` or
  over `max_growth` x its recorded cold size.
- Fail closed to cold, loudly: a clean-exit marker (`state.json`) must be present
  at start, else the root is wiped (`wiped(unclean-previous-exit)`); epoch change,
  identity mismatch, dockerd refusing to start on the persistent root (retry once
  on a fresh ephemeral root, wipe the broken one) are all named outcomes. The
  outcome `{outcome, bytes, ...}` is printed in `command.log` and written to
  `docker-cache.json` in the attempt output; the worker adds it to the attempt
  result as `docker_cache`. The cache never fails a job.
- Cold canary: every `canary_every`-th attempt (deterministic from the attempt id)
  and the first on an empty root bypass the cache. A handler may write
  `docker-cache-fingerprint.json` (`{inputs, outputs}`) in its output; attempts of
  the same inputs digest must have equal outputs warm and cold. A mismatch purges
  the namespace root and records `canary_mismatch`.
- Only Linux rootless Docker backends (`rootless-docker`, `rootless-docker-native`)
  use it. macOS, Windows and non-Docker handlers report `disabled`.

## Impact

Code: `workloads/docker_cache.py` (new), `docker_command.py`, `docker_native.py`,
`docker_runtime.py`, `supervision.py`, `worker.py`. Docs: `node-py/docs/docker-build-cache.md`.
Rollback: `enabled:false` (or remove the block) and restart the worker when idle;
the directory is removed with the documented purge command.
