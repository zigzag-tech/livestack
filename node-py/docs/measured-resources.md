# Measured resources

How an attempt's resource use is measured, what survives a kill, and how to check the
instrument itself. Design: `openspec/changes/measured-resource-declarations/`.

## What survives an OOM kill (measured 2026-10-08)

`node-py/scripts/probe-unit-cgroup-after-oom.sh direct|child` launches a transient
`--user` unit shaped like `supervision.py` (`Type=exec`, `OOMPolicy=kill`, `MemoryMax=64M`,
`MemorySwapMax=0`, `KillMode=control-group`) that allocates 256 MiB, then polls.

Result on systemd 259.5, kernel 7.0.0-38, cgroup2, both shapes (main process allocates;
wrapper spawns an allocating child):

| What | After `ActiveState=failed` (about 200 ms after the kill) |
|---|---|
| cgroup directory (`memory.events`, `memory.peak`, `pids.*`) | **gone** (never readable at the flip) |
| `Result` | `oom-kill` |
| `OOMKills` | `1` |
| `MemoryPeak` | `67108864` (the limit) |
| `CPUUsageNSec` | retained |
| `TasksCurrent`, `IOReadBytes`, `ControlGroup` | `[not set]` / empty |

Consequences: the OOM verdict comes from the retained unit properties, not a late
cgroup read; tasks and disk evidence come only from the last worker-loop sample. Re-run
the probe on any new systemd major version or on a new OS before relying on this.

## The pipeline

1. The worker loop samples the attempt cgroup every turn (`resource_usage.sample`:
   `memory.events` `oom_kill`, `memory.peak`, `pids.events`/`pids.peak`, `cpu.stat`), and the
   workspace filesystem's used-bytes delta at most every 5 s (`disk_delta_bytes`). The delta is
   `statvfs`, filesystem-wide: it is an UPPER BOUND on the attempt's own writes, so it is
   recorded but never audited, and the `disk` limit cause fires only on an infrastructure end
   whose delta reached the declared `need.disk_bytes`.
2. `resources.source` says where figures came from: `receipt` (the wrapper's exit receipt, the
   last sample filling only what it lacks), `unit` (the failed unit's retained properties) or
   `sampled`. `resource_evidence: "none"` means nothing could be read; no figure is zero-filled.
3. `store.complete` types a breach as `resource_limit` (non-retryable, no automatic raise of the
   declared need) and records succeeded and resource-limited attempts in `resource_history`
   (at most 50 rows per handler and dimension, 30 days; derived data, rebuilt at start from the
   last attempts when the table is empty; infrastructure outcomes are not evidence).
4. `GET /status` and `GET /workers` (roster) carry `resource_audit.flags`: `declared_below_observed`
   (with `suggested`, which is only text, never applied), `admit_below_typical`, and the info-only
   `declared_far_above_observed`. Fewer than 5 samples never flags. Memory is judged on the
   non-reclaimable peak when present, the cache-inclusive cgroup peak otherwise.
5. Optional floor, authority config section (absent = off), reloadable with SIGHUP:

       "resource_floor": {"handlers": ["compile.*"], "margin": 1.15, "min_samples": 5,
                          "history_max_age_seconds": 2592000, "strict": false}

   A submit whose `need.memory_bytes` is below `ceil(max(observed max, p95) x margin)` for a
   covered handler is refused 422 `resource_floor: ...` naming the figures. Unreadable history
   disables the floor for that request and shows `resource_audit.floor` as `unavailable: ...`,
   unless `strict` (then 503 `resource_floor_unavailable`). There is no per-handler ceiling in
   the authority, so the design's `floor_exceeds_ceiling` refusal does not exist.
6. Metrics: a result's `metrics` keys must be defined in `metrics_schema.BUILTIN` or in the
   handler release manifest's optional `metrics: [{name, unit, measures, excludes}]` (which is part
   of the release digest only when present). Undeclared or mis-scoped (a sub-phase above its whole)
   values are dropped and counted in `GET /status` `metrics` (counters since process start).

## Measure the instrument (checklist before trusting a number)

- Does a test move the quantity by a known amount and see the reading move? (CPU busy vs idle,
  memory allocate, bytes written; `tests/test_workload_worker.py`.)
- Is "not measured" distinguishable from zero in that test?
- Does the control fail on the code before the change? (The OOM control re-queued the job on
  `origin/main`.)
- What does the number EXCLUDE, and is that written next to its name?
- Does a value that is supposed to be a part ever exceed its whole?
