# Single model store per host, and per-worker cache bounds

**Rule.** A host has ONE model cache: the shared Hugging Face store (`~/.cache/huggingface`, hub under
`hub/`). A worker's workspace and a job's environment never get their own `HF_HOME` / `HF_HUB_CACHE`.
Evidence: a one-off manual FLUX.2-dev download inside an e2e workspace (2026-10-28) duplicated 166 GB that
the shared store already held.

## Worker config (`store_guard`, opt-in; absent = today's behaviour)

```json
"store_guard": {
  "model_store": "/home/ubuntu/.cache/huggingface",
  "max_repo_bytes": 2147483648,
  "scan_seconds": 600,
  "sweep": {"incomplete_max_age_seconds": 86400, "scratch_max_age_seconds": 86400,
            "scratch_cap_bytes": 21474836480}
}
```

- Every job gets `HF_HOME`, `HF_HUB_CACHE` and `HF_HUB_OFFLINE=1` pointing at `model_store`
  (`_attempt_env`). Downloads are done by operators into the shared store, never by a job.
- **Startup fails** (`single_store_violation`) when the worker's own `environment` points `HF_HOME`,
  `HF_HUB_CACHE`, `HUGGINGFACE_HUB_CACHE` or `TRANSFORMERS_CACHE` into the workspace, or when `model_store`
  is inside the workspace.
- **Guard.** Every `scan_seconds` (and once on each attempt tree before it is removed) the workspace is
  scanned (depth 6, 200k entries, symlinks not followed) for `models--*` / `datasets--*` / `spaces--*`
  directories. One that also exists in the host store is `duplicate_of_host_store`; one above
  `max_repo_bytes` is `over_bound`. Each is logged once at ERROR as `single_store_violation: kind=... path=...`
  and written to `<state_dir>/single-store-violations.json`. It is reported, not deleted (it may be a live
  download).
- **Sweep (Rule 10 enforcer).** Same tick, workspace only, never the host store: `*.incomplete` files older
  than `incomplete_max_age_seconds`; `flux2-offload-*` scratch dirs older than `scratch_max_age_seconds` or
  beyond `scratch_cap_bytes` (newest kept). Logged as `store_sweep:`.

Code: `livestack_node/workloads/store_guard.py`; tests: `tests/test_store_guard.py`.

## Bounds inventory for per-worker caches

| Cache | Bound | Enforcer |
|---|---|---|
| `input-cache-*` in the workspace | `input_cache_bytes`, 32 entries, 14 d | `InputCache.prune` each step |
| `flux2-offload-*` scratch, `*.incomplete` | 24 h, 20 GiB cap | `store_guard` sweep |
| Docker data-root `~/.cache/livestack-docker-cache/<worker>/<principal>` | `docker_cache.max_bytes` per namespace (host aggregate: see openspec `docker-data-root-consolidation`) | `docker_cache.prune` / `Session.finish` |
| Dependency cache `~/.cache/livestack-dependency-cache/<worker>/<principal>` | `dependency_cache.max_bytes` per namespace, LRU | `DependencyCache._evict` |
| Duplicate bytes across dependency caches | none (copies) | `python3 -m livestack_node.workloads.cache_dedupe [--apply] ROOT...` hardlinks byte-identical files (full sha256 + byte compare, atomic rename, files younger than 10 min skipped) |

Measured 2026-11-06 (dependency caches, sampled head/mid/tail for the estimate, full hash for the tool's
dry run, files >= 64 KiB): zz-joe 54 GB inode-unique, 43.4 GB saved by the tool (35,959 files); zz-whitby 3.8 GB, 2.4 GB saved (2,167 files).
Docker layers: zz-joe 24% redundant (35.6 of 149.5 GB layerdb bytes), zz-whitby 8%.

## Rollout and rollback

Code is inert until a worker config gains `store_guard`. Roll out: land, update the worker package on a
host, add `store_guard` to one worker's json, restart that worker only when it has no running attempt
(authority `workloads.sqlite` read-only), watch for `single_store_violation` / `store_sweep` in the journal.
Rollback: remove the `store_guard` key and restart the worker; nothing else changes. `cache_dedupe` rollback:
hardlinks are indistinguishable from copies to every reader; to split them back, `cp --remove-destination`
the file, or simply let the cache evict (entries are removed whole).
