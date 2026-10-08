## 1. State ownership

| State | Owner | Bound |
|---|---|---|
| Effective bound + floor (computed) | authority process, recomputed on a timer and on SIGHUP | derived, not stored; last value logged and in status |
| Filesystem free/total | `statvfs` of the blob root, workspace, state dir, read at most every `refresh_seconds` (default 15, floor 5) | n/a |
| Retention tiers / TTL rules | authority config, schema validated, reloadable | fixed set of tiers, rule list <= 64 entries |
| Unbounded-owner report | derived by one grouped query per status | <= 20 rows shown, total count and bytes always |
| Worker `disk_unavailable` | worker report, authority stores in the `workers` row | fixed keys |
| CPU self-test result | worker memory, in report | one record |

Ids cross as: owner string (blob), job id, worker id. No new table is required except
none; tiers apply by column predicate on existing job/attempt rows.

## 2. Effective bound

```
effective_max_bytes = min(max_bytes,                       # absolute cap, today's value
                          floor(capacity_fraction x fs_total))   # optional
floor_bytes         = max(headroom_bytes, ceil(headroom_fraction x fs_total))
admit put of size s iff  used + s <= effective_max_bytes
                     and fs_free - s >= floor_bytes
```

Config (`storage_bounds`, absent = today's behaviour, only the cap applies):

```json
"storage_bounds": {"objects": {"capacity_fraction": 0.5, "headroom_bytes": 42949672960,
                               "headroom_fraction": 0.1, "alert_fraction": 0.15},
                   "refresh_seconds": 15}
```

Validation: fractions in (0, 1]; `alert_fraction >= headroom_fraction`; bytes > 0; unknown
keys fail closed; a `max_bytes` above `capacity_fraction x fs_total` is **not** an error
(it is the point) but is logged as an effective-bound change. If `statvfs` fails, admission
**refuses with `storage_headroom_unknown`** for puts above a small allowance (default 1 MiB)
and says so; it never assumes plenty of space.

The bound is logged as one line `storage bound objects: effective=<n> (cap=<n>,
fraction=<n>) floor=<n> free=<n> fs_total=<n>` at startup, on SIGHUP, and when the
effective value or the state changes by more than 1 %. The same record is status.

## 3. Proactive GC before refusing

On a put that fails the free-space test the authority, holding the existing
store-serialised reservation lock, runs `BlobStore.prune()`-equivalent once with a
`deficit` target: expire references by tier rule, delete unreferenced ready blobs oldest
first (never referenced, never `uploading`), stop once `fs_free - s >= floor`, bounded to
`gc_batch` (default 256) deletions per pass and at most one pass per `refresh_seconds`
(a refusal storm must not become a GC storm; subsequent puts in the window see the
recorded result). The refusal carries what the pass freed. Referenced bytes are never
deleted to make room; if only referenced bytes remain the refusal says so
(`all remaining bytes referenced`) and names the largest owners/prefixes (top 5).

## 4. Tiered retention

Today: `retention_seconds` (blobs, 14 d, by last use), terminal jobs flat (3 d),
`reference_expiry` rules `{owner, prefix, keep_newest, min_age_seconds}`.

Proposed `retention_tiers` section:

```json
"retention_tiers": {
  "jobs": {"succeeded_seconds": 259200, "failed_seconds": 1209600, "cancelled_seconds": 86400},
  "references": [{"owner": "benchday-release", "prefix": "benchday.release.",
                  "keep_newest": 10, "ttl_seconds": 1209600}]
}
```

- Failed outcomes are kept longer than succeeded ones (they are what a human debugs);
  floors: every window >= 1 h; each unset tier keeps today's flat behaviour.
- A reference rule keeps the newest `keep_newest` per (owner, prefix) **and** drops
  anything older than `ttl_seconds`; both are required, so "never expires" cannot be
  written by accident. `ttl_seconds` may be `null` explicitly only with
  `keep_forever_acknowledged: true` (an operator can say never; it cannot be the default).
- Retained-on-purpose is a flag on the reference (`retain: true`, set by the owner API,
  not by the TTL) and exempts it from TTL but counts toward a bounded `retained_bytes`
  figure in status.
- Unmatched owner/prefix references are summarised as `unbounded_references`.
- The compatible subset: today's rule keys continue to validate; `min_age_seconds` maps to
  `ttl_seconds`.

Retention **values** for the production authority remain the other agent's change; this
change ships the mechanism and the documentation default for fresh installs.

## 5. Worker disk honesty

`report()` already computes `available.disk_bytes`. It additionally emits

```
disk_unavailable: {filesystem, free_bytes, reserve_bytes, offered_bytes, capacity_bytes,
                   reason: "reserve_exceeds_free" | "backing_reserve" | "below_need"}
```
whenever `offered_bytes < capacity_bytes` because of a reserve. Placement turns a
disk-driven refusal into `worker <id>: disk offered X of Y (free F < reserve R)` instead
of `insufficient shared host resources` (the memory path is already specific). A worker
whose reserve is >= free logs a warning at startup and every report-state change (not
every report) and the roster shows `reserve_exceeds_free`. Whether to auto-reduce the
reserve is **not** done: the operator owns it (open question 2).

## 6. CPU signal

Facts established on this fleet: load average counts uninterruptible tasks and is unbounded
relative to cores; `/proc/pressure/cpu` `some` populates, `full` reads 0 on this kernel.
Policies:

- `psi_some`: stalled when `some avg10 >= stall_some_avg10_percent` (default 40).
  avg10, not avg60, so a finished burst releases admission within ~10 s.
- `runqueue`: mean of `procs_running` minus the sampler's own thread over `window_seconds`
  (default 5, 1 Hz), per core; stalled when above `stall_runnable_per_core` (default 2.0).
- Both offer `capacity - reserve_cpu` when not stalled, 0 plus a named reason when stalled,
  exactly as `psi` does today; placement's reservations decide fit.

Self-test (positive control at startup and on config reload): measure the signal idle,
spin `cores + 1` busy workers for `selftest_seconds` (default 3, bounded 1-10), measure
again; the policy is `active` if the signal rose by at least a minimum delta, else
`inert`. An `inert` policy reports `cpu` unavailable with `cpu_signal_inert: <policy>` and
logs once. It never falls back silently to another policy. Test seam: `psi_path` and a
`/proc` root path for fakes (the existing seam). Because the self-test burns CPU on a
busy host, it is skipped (with `selftest: "skipped, host busy"`) when the signal is
already above threshold, and the policy is then `active_unverified`, shown as such.

## 7. Refusals as status

`placement` already builds `rejected: [{worker, reason}]`. Add stable `reason_code`
(`disk_reserve`, `disk_need`, `memory_claim`, `host_memory_pressure`, `cpu_stalled`,
`capability`, `storage_headroom`) and numeric figures to each entry and aggregate in the
roster as "N jobs waiting: M for disk on <worker> (offered 0)". A job waiting more than
`stall_report_seconds` (default 300) because of the same reason code on all candidate
workers raises one bounded event, so a stall like the stager's 20 minutes is announced,
not discovered.

## 8. Positive controls

- headroom: a fake filesystem (`statvfs` seam) at free = floor + 1 byte accepts; at free =
  floor - 1 refuses with the named reason; the control **fails on `origin/main`**, which
  admits both.
- GC: seeded unreferenced blobs allow a put that first failed; referenced-only store
  refuses naming owners; second put in the window does not re-run GC (counter).
- tiers: seeded failed vs succeeded rows at ages straddling their windows; reference
  rules with keep_newest and ttl; `unbounded_references` appears for an unmatched owner.
- worker disk: reserve 64 GiB over free 60 GiB yields `disk_unavailable` and the named
  placement reason; shrinking the reserve clears it.
- cpu: real synthetic burn moves `psi_some`/`runqueue` and not `full`; a fake `psi_path`
  frozen at 0 makes the policy `inert` and report refusal, not fallback.
- statvfs failure refuses large puts with `storage_headroom_unknown`.

Tests that need a real cgroup or kernel PSI skip with an explicit reason, never pass.

## 9. Risks

- Floor too high locks the store: floor is derived from config and status shows it; the
  refusal and the `low` alert make it obvious. A bad config fails closed at startup.
- GC under the reservation lock lengthens puts: bounded batch and one pass per window.
- Self-test burns CPU: short, bounded, skipped when busy.
- Tier rules misconfigured to delete too much: floors on windows, `keep_newest >= 1`,
  dry-run endpoint `GET /retention/plan` reports what a pass would delete (counts, bytes)
  without deleting; first enabling is intended to be preceded by it.

## 10. Open questions for the owner

1. Choose defaults for the objects store: `capacity_fraction` and `headroom` (proposed:
   fraction 0.5, floor max(40 GiB, 10 %)). The tower's 210 GiB would become about
   half the disk; do you want less?
2. Should a worker whose `disk_reserve_bytes` exceeds free space auto-lower it (not
   proposed), or only announce it (proposed)?
3. Failed-job retention longer than succeeded (proposed 14 d vs 3 d): acceptable cost?
4. Release references: `keep_newest` 10 and `ttl` 14 d proposed for benchday.release.*;
   confirm with the retention agent so values are set once, in `authority.json`.
5. `runqueue` vs `psi_some` as the recommended default policy (proposed `psi_some`,
   `runqueue` as the fallback where PSI is unavailable).
