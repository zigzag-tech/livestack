# Storage headroom, tiered retention, disk honesty

Mechanism for `openspec/changes/storage-headroom-admission`. The VALUES for a production
authority live in its `authority.json`; absent sections keep today's behaviour exactly. Both
sections are schema-validated (unknown keys fail closed at startup and on SIGHUP, no value is
echoed) and reload on SIGHUP with the principals.

## `storage_bounds` (authority.json)

```json
"storage_bounds": {"objects": {"capacity_fraction": 0.5, "headroom_bytes": 42949672960,
                               "headroom_fraction": 0.1, "alert_fraction": 0.15},
                   "refresh_seconds": 15, "unknown_allowance_bytes": 1048576, "gc_batch": 256}
```

- Effective bound = `min(blob_limits.max_bytes, floor(capacity_fraction x filesystem size))`.
- Floor = `max(headroom_bytes, ceil(headroom_fraction x filesystem size))`. A put is admitted iff
  `free - size >= floor` (exactly at the floor is admitted, one byte less is refused).
- `alert_fraction` (default 1.5 x floor) is the `low` threshold. States: `ok`, `low`, `refusing`,
  `unknown` (statvfs failed: puts above `unknown_allowance_bytes` are refused as
  `storage_headroom_unknown`; room is never assumed).
- Free space is read fresh on every put that is not already stored; `refresh_seconds` only paces
  the GC and the bound's log line. Concurrent partial uploads are not pre-counted: the floor is a
  soft bound by up to one in-flight object per concurrent upload (max 16).
- Refusal is HTTP 507 `storage_headroom: objects filesystem has X GiB free, floor Y GiB (after GC
  freed Z GiB)`, with `; all remaining bytes referenced (largest owners: ...)` when nothing was
  deletable. Upload grants consult nothing themselves; the PUT they authorise does.
- GC (`BlobStore.collect`): at most once per `refresh_seconds`; expires references by rule, then
  deletes unreferenced ready objects not used for an hour, least recently used first, at most
  `gc_batch`, until the deficit is freed. Never deletes a referenced object.
- Logged line on startup, reload and change >1 %: `storage bound objects: effective=... floor=...
  free=... state=...`. The last 8 state changes are in status `storage.events`.

## `retention_tiers`

```json
"retention_tiers": {"jobs": {"succeeded_seconds": 259200, "failed_seconds": 1209600,
                             "cancelled_seconds": 86400},
                    "references": [{"owner": "benchday-release", "prefix": "benchday.release.",
                                    "keep_newest": 10, "ttl_seconds": 1209600}]}
```

- Job windows (>= 1 h) per terminal outcome (`expired` shares `cancelled`); an unset tier keeps the
  flat `limits.terminal_seconds`. `limits.terminal_jobs` still bounds the count.
- A reference rule matches `owner` and name `prefix` (longest prefix wins). A reference is removed
  only when it is beyond the newest `keep_newest` of its rule AND older than `ttl_seconds` (age =
  last `replace`; legacy rows start their clock at the upgrade). `ttl_seconds: null` needs
  `keep_forever_acknowledged: true`. `min_age_seconds` is accepted as the old spelling of the ttl.
  Table `blob_reference_ages(owner,name,updated,retain)`; `retain=1` exempts (no API sets it yet).
- References matched by no rule are `unbounded_references` (count, bytes, top 20 owners) in status.

## Endpoints (admin principal)

- `GET /v1/workloads/status` -> adds `storage`: objects, used bytes, `bound` (effective bound, floor,
  free, state), `events`, `last_gc`, `unbounded_references`, `retained_bytes`, `would_expire`.
- `GET /v1/workloads/retention/plan` -> dry run, deletes nothing: `jobs.would_delete/by_state`,
  `references.would_expire`, `references.unbounded_references`, `unreferenced_blobs`.
- `GET /v1/workloads/workers` -> adds top-level `storage` (the bound) and per-worker
  `disk_unavailable`, `reserve_exceeds_free`, `cpu_signal`, plus warnings `reserve_exceeds_free`
  and `cpu_signal_inert`.

## Worker disk and placement

A worker whose `disk_reserve_bytes` (or `backing_reserve_bytes` on a backing filesystem) leaves it
offering 0 disk reports `disk_unavailable {filesystem, free_bytes, reserve_bytes, offered_bytes,
capacity_bytes, reason}` and logs the change once. Placement then refuses a disk-needing job with
`worker <id>: disk offered 0.0 of 192.0 GiB (free 60.0 GiB < reserve 64.0 GiB, reserve_exceeds_free)`,
recorded as `{reason_code: "disk_reserve"|"disk_need", figures}` in the job's reason. The reserve is
never lowered automatically (the operator owns it). A job refused for one code by every candidate
worker for `limits.stall_report_seconds` (default 300) logs one `placement_stalled` warning.
Rollout order: authority first (it accepts the new report keys), then workers.

CPU signals: `node-py/docs/worker-cpu-admission.md`.
