## State ownership

- Persistent root `<path>/<ns>/slot`, lock `<path>/<ns>/slot.lock`, state
  `<path>/<ns>/state.json`, canary records `<path>/<ns>/canary.json`. `<ns>` is
  `<slug(owner)>-<sha256(owner)[:12]>`. The directory `<path>` is created 0700 by
  the worker's uid and must be on a filesystem that supports overlay2 and outside
  the workspace. Owner of all of it: the namespace process (`docker_command`),
  holding the flock for the attempt. The worker process only passes settings in
  and reads the outcome out; it never touches the root while an attempt is live.
- Files below `slot` are owned by sub-uids on the host. Everything that must read
  or delete them (size walk, wipe) runs inside the RootlessKit namespace, i.e. in
  `docker_command`, where uid 0 maps over them. Purge from outside uses
  `rootlesskit rm -rf` like `remove_data`.

## Locking

`flock(LOCK_EX|LOCK_NB)` on `slot.lock`. The kernel releases it when the holder
dies, so a crashed attempt can never leave a stale lock; the holder record
(pid, unit) inside is diagnostic only. Child processes do not inherit the fd
(PEP 446). Busy lock -> ephemeral root, outcome `cold-locked`.

## Why not trust a root that was not stopped cleanly

dockerd/BuildKit/containerd metadata is not crash-safe across SIGKILL of the
cgroup (timeout, lease loss, OOM). `state.json` has `clean:false` while an
attempt holds the root and `clean:true` only after dockerd stopped and size
guards passed. Unclean -> wipe. This costs one cold attempt after a kill, in
exchange for never booting a corrupt store.

## Bound and its enforcer

Named bound: `docker_cache.max_bytes` per namespace root. Enforcer: end-of-attempt
sequence in `docker_cache.finish` (docker still up): `docker rm -f` all
containers, `docker volume prune -f`, `docker network prune -f`,
`docker image prune -f` (dangling), `docker builder prune -f --keep-storage
max_bytes`; if `docker system df` is still over, `docker image prune -a -f`
(unused images; base images re-pull). Hard guard after dockerd stops: walk the
root (lstat sum, no symlink following); over `max_bytes` or over `max_growth` x
`cold_bytes` -> rmtree, outcome `discarded(size)`. `cold_bytes` is the size
recorded after the first attempt on an empty root. `until=` filters are not used:
`until` is image creation time, which would evict old base images first.

## Canary

Deterministic: `int(sha256(attempt_id)) % canary_every == 0`, and always when the
root is empty. A canary attempt runs on an ephemeral root (exactly today's cold
path) and afterwards compares its fingerprint with the most recent warm record
sharing its `inputs` digest, taking the root lock briefly (skipped if busy). Warm
attempts record theirs, and compare against recorded cold ones the same way.
Mismatch in `outputs` -> purge the root, write `canary_mismatch` into the
outcome. Only fingerprints that name their `inputs` are comparable and the job's
own exit verdict is never compared (two runs of one tree legitimately differ;
the first rollout purged a good root by comparing verdicts).

## Results

`docker-cache.json`: `{outcome: hit|cold-new|cold-canary|cold-locked|wiped|discarded|disabled, reason, bytes, cold_bytes, namespace, epoch, canary, prune}`.
The worker copies it into the attempt result under `docker_cache`.

## Failure behaviour

Any exception in cache logic (not in the job) is caught, logged as
`docker_cache_error` with its type, and the attempt proceeds on an ephemeral root
or finishes without cache bookkeeping; the job result is unaffected.
dockerd failing to become ready on a persistent root: wipe it, restart dockerd
once on an ephemeral root, outcome `wiped(dockerd-start-failed)`.
