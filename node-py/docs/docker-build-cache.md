# Persistent Docker build cache for rootless workers

Status: implemented 2026-10-08 (openspec `docker-build-cache`). Code: `livestack_node/workloads/docker_cache.py`,
wired in `docker_command.py`, `docker_native.py`, `docker_runtime.py`, `supervision.py`, `worker.py`.

## What it does

A rootless-Docker attempt normally gets a private dockerd whose data root (`<attempt>/docker-data`) is deleted with
the attempt, so no layer, image or `RUN --mount=type=cache` survives. With `docker_cache` enabled the data root
is `<path>/<namespace>/slot` instead and survives, so BuildKit's layer cache, cache mounts and pulled base images
work natively for any app. Nothing app-specific is declared.

## Config (worker.json, no environment variables)

    "docker_cache": {"enabled": true, "path": "/home/ubuntu/.cache/livestack-docker-cache/<worker>",
                     "max_bytes": 42949672960, "epoch": 0, "canary_every": 20, "max_growth": 3.0}

- `path` absolute, private (0700, owned by the worker uid), on a filesystem that supports overlay2, OUTSIDE the
  workspace. Give every worker its OWN path (two workers on one path will find the lock busy and run cold).
- `max_bytes` the bound (64 MiB .. 4 TiB). `epoch` integer; bump = every namespace starts cold next attempt.
  `canary_every` N (0 = only the first attempt on an empty root is a cold reference). `max_growth` factor.
- Missing, `enabled:false` or any invalid key = disabled = exactly the old behaviour; an invalid block is logged
  (`docker_cache disabled: invalid: ...`) and the attempt result says `docker_cache: {outcome: disabled, reason}`.
- Only `rootless-docker` / `rootless-docker-native` handlers on Linux. macOS and Windows workers report
  `disabled (unsupported-platform)`.

## Namespace and isolation

`<path>/<slug(owner)>-<sha256(owner)[:12]>/` where `owner` is the assignment's submitting principal
(`assignment['owner']`, NOT the user-settable `labels.owner`). Different principals never share a root. Inside:
`slot/` (dockerd data root), `slot.lock` (flock), `holder.json`, `state.json`, `canary.json`.

## Safety

- One live dockerd per root: exclusive `flock` held by the namespace process for the whole attempt. The kernel drops
  it when the holder dies, so a crash cannot leave a stale lock. Busy lock -> attempt runs on an ordinary ephemeral
  root, outcome `cold-locked`.
- `state.json.clean` is false while an attempt holds the root and true only after dockerd stopped by itself and the
  size guards passed. Unclean (timeout/OOM/lease loss kill) -> `wiped(unclean-previous-exit)`.
- Epoch change, state identity mismatch, dockerd failing to start on the root -> wiped, attempt runs cold
  (`wiped(epoch|identity-mismatch|dockerd-start-failed)`). A cache fault never fails a job.
- `remove_data()` only ever removes `<workspace>/<attempt>/docker-data`; a persistent root is never named.

## The bound and its enforcer (Benchday rule 10)

Bound: `docker_cache.max_bytes` per namespace root. Enforcer: end of every attempt, dockerd still up
(`docker_cache.prune`): remove containers, `volume prune`, `network prune`, `image prune` (dangling),
`builder prune -f --keep-storage 0.7*max_bytes` (headroom for images and one attempt's growth); if `docker system df` still exceeds the bound, `image prune -a`.
After dockerd stops the root is measured (allocated blocks, hard links once) and DISCARDED if over `max_bytes`
or over `max_growth` x its cold size, default 3 (`discarded(size|growth|unmeasurable)`; a walk that cannot read a subtree fails closed). Measured on Benchday's image: cold root 22 GB, +7.5 GB per attempt before LRU pruning. Worst case on disk is therefore about
one in-flight attempt beyond `max_bytes` per worker, plus a wipe window. No `until=` filter is used: it is image
creation time and would evict old base images first.

## Outcome (never silent)

`command.log` gets `docker_cache: {...}`, the attempt output gets `docker-cache.json`, and the attempt result gets
`docker_cache`: `outcome` is one of `hit`, `cold-new`, `cold-canary`, `cold-locked`, `cold-unsafe`, `cold-error`,
`wiped`, `discarded`, `disabled`; plus `reason`, `bytes`, `cold_bytes`, `prune`, `canary_mismatch`.

### What `seconds` measures

`seconds` is the wall time the cache itself cost: `phases.begin + phases.prune + phases.finish`.
`phases` also lists `wipe` (inside begin/finish), `walk` (size walk, inside finish), `dockerd_start` and
`dockerd_stop` (paid with or without a cache, so not counted), and `prune.steps` has each docker prune
command. `session_seconds` is the whole attempt inside the launcher. Before 2026-10-08 `seconds` was
that whole-session number: it was ~95% of attempt wall time (median 277 s) and was misread as cache
overhead. Measured overhead is seconds, not minutes: the size walk of a 34 GB root takes under 1 s and
the whole post-job tail (handler exit to dockerd SIGTERM) is ~21 s even on a cache-less canary.

## Cold canary

Every `canary_every`-th attempt (sha256 of the attempt id mod N) runs on an ephemeral root, exactly the old cold
path, and the first attempt on an empty root is the cold reference. A handler may write
`docker-cache-fingerprint.json` (`{"inputs": "<digest>", "outputs": {...}}`) into its output directory. Warm and
cold records with equal, NAMED `inputs` must have equal `outputs`; otherwise the root is purged, the outcome is
`wiped(canary-mismatch)` and `docker_cache_canary_mismatch: {...}` is logged. A job's own verdict is deliberately
NOT compared (two runs of one tree legitimately differ), and attempts without a fingerprint are never compared:
the first deployment compared exit codes of unrelated attempts and purged a good root. Benchday emits the
fingerprint from `buildNodeImage` (see its `docs/e2e-build-cache.md`).

## Native frontend and slow bookkeeping

In `rootless-docker-native` the host frontend stops dockerd after the handler returns; the namespace process then
walks the root, runs the canary and wipes. The frontend waits up to `FINISH_WAIT` (900 s, announced in
`docker-cache-session.json`) for it instead of the old 5 s, because killing it there lost the outcome record
(seen in the first rollout). A wipe renames the slot aside first, so a kill mid-delete leaves only a
`slot.trash-*` sibling that the next attempt removes.

## Operations

    # state of a worker's cache
    ls -la <path>/*/ ; cat <path>/*/state.json
    # purge one namespace (worker idle): files are sub-uid owned, so use rootlesskit
    rootlesskit --state-dir=$(mktemp -d /run/user/$UID/hpurge-XXXX) rm -rf <path>/<namespace>
    # purge everything on a worker: bump `epoch` in worker.json and restart the worker (idle), or purge as above
    # Roll back: set "enabled": false (or delete the block), restart the worker WHEN IDLE, purge <path> as above.

Roll out a worker release per `worker-release-rollout.md`; config keys survive a release roll. The directory is
counted by the disk reaper like any other: do not place it under a path `diskreap` treats as a cache.

## Limits

The apt/curl/npm layers of an app are cached forever until the Dockerfile changes, the root is wiped or `epoch`
bumps; an app that needs refresh should bump an `ARG`. A BuildKit instruction that is best-effort (a download
that may fail) must not live in a cached layer: use `--no-cache-filter` for that stage.

## Rollout record and measurements (2026-10-08)

Release `livestack-925a02fa` (code of main 925a02fa), drop-in `95-docker-cache.conf` per worker (rollback: delete
it, `daemon-reload`, restart when idle; and set `docker_cache.enabled` false or restore `worker*.json.bak-dockercache-*`).
Cache dir `~/.cache/livestack-docker-cache/<worker>`, bound 40 GiB per namespace (WSL 30 GiB), `canary_every` 20
(zz-joe-e2e-3: 3). Drain/enable with the atomic `claim_enabled` edit in `worker-release-rollout.md`.

Enabled slots (2026-10-08): zz-joe-e2e-1, -2, -3, -4 and -5 (cache dir named for the worker; e2e-5 configured by a
separate rollout), xc-win-1-wsl and xc-win-1-wsl-2 (each its own `<worker>` dir, 30 GiB). zz-joe-e2e-2 runs the
taskenv release (livestack-2151b9d4, same `docker_cache.py`/`docker_command.py` as 925a02fa) so it needs no
PYTHONPATH change; xc-win-1-wsl-2 got the `zzz-docker-cache.conf` drop-in (925a02fa). Backups:
`worker-2.json.bak-dockercache-*`.

zz-joe, Benchday E2E attempts of 2026-10-08 (command.log `Built benchday/e2e-node:<fp> in Ns`, authority DB):

| class | attempts | image build median | start -> postgresReady median |
|---|---|---|---|
| before (no cache) | 125 | 716 s | 1010 s |
| cold-new (first on a root) | 3 | 695 s | 908 s |
| hit (same image fingerprint) | 8 | 2 s | 117 s |

A hit with a NEW source tree (one-crate bump) measured in isolation: 241 s vs 582 s cold. Root size 22.9 GB per
slot after the first build.

Incident during rollout (fixed in 925a02fa): the first release compared job verdicts in the canary and purged a good
root, and the native frontend killed the attempt process 5 s after dockerd stopped, before the outcome was written.
Both failed closed (next attempt wiped and ran cold).
