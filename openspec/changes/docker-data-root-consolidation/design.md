## Context

`docker_cache.py` already bounds one namespace (`max_bytes`, 70% prune, discard on size/growth). It has no
host-level view: each worker config carries its own `path` and the sum is unconstrained.

## Decisions

1. Host aggregate first. It is the Rule 10 gap and needs no new storage layout. The enforcer runs inside
   `Session.begin` under the namespace lock it already takes; it measures sibling namespaces by their
   `state.json` `bytes` (already recorded at finish), never by walking them, and removes only namespaces
   that are unlocked (`slot.lock` free) and not the requesting one, oldest `last_attempt` first.
2. Shared base-image store second. Build a read-only image store directory per host from the images the e2e
   warm step already pulls, and reference it from each slot's rootless `storage.conf`
   (`additionalimagestores`). Layers are then resolved from the shared store and never copied into a slot.
   Gate: the canary (`canary_every`) must pass cold and warm, and slot size after warm must drop by at
   least the measured base-image size before it is enabled on a second worker.
3. Build-cache snapshots (about 60% of bytes) stay per-namespace; the only lever is a lower `max_bytes`
   chosen from the observed `cold_bytes` in `state.json` (zz-joe: cold 23 GB, steady 33 to 37 GB, so 40 GiB
   is already tight; do not lower it without data).

## Risks

A shared store that is rewritten while a slot reads it corrupts that slot. The store is built into a new
directory and switched by atomic symlink; old directories are removed only when no slot lock names them.
Rollback: remove the `additionalimagestores` line and the host cap; slots re-pull what they need.
