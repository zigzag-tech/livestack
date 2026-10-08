## 1. State ownership

| State | Owner | Bound |
|---|---|---|
| Live cgroup sample during an attempt | the worker process, in memory | one record per attempt, fixed keys |
| Final `resources` block | attempt result row (authority sqlite) | existing result size bound; keys fixed by schema, unknown keys dropped and counted |
| Per-handler history | authority sqlite table `resource_history(handler, dimension, attempt, value, outcome, at)` | last `HISTORY_WINDOW` (default 50) rows per (handler, dimension), enforced in the same transaction as insert; plus an age cap (`resource_floor.history_max_age_seconds`, default 30 d, floor 1 d) |
| Floor policy | authority config file section `resource_floor`, validated by pydantic (`extra=forbid`), reloadable on SIGHUP | n/a |
| Metric definitions | `metrics_schema.py` (code, versioned) | fixed set |

No environment variables. The attempt id is the key across worker and authority; the
history table is derived data and may be rebuilt from attempt results, so losing it
degrades to "no history" (stated), never to wrong numbers.

## 2. Sampling that survives the kill

Today `resource_usage()` runs inside the wrapper at exit; if the wrapper dies with the
cgroup (kernel OOM of the unit), no receipt exists, and the worker's loop sees
`ActiveState=failed` and raises `execution stopped without a result` (503), or the lease
keeper expires first (`execution lease lost`). The worker loop already reads the
cgroup each 0.2 s turn for the non-reclaimable peak (`cgroup_nonreclaimable`). The same
turn additionally reads (cheap, each file a single bounded read, the existing
`values()` helper):

- `memory.peak`, `memory.events` -> `oom_kill`, `oom`, `max`
- `pids.peak`, `pids.events`
- `cpu.stat` -> `usage_usec`
- disk: bytes used under the attempt workspace, at most every 5 s (a `statvfs` delta
  against the workspace baseline taken at attempt start, not a tree walk)

The last successfully read sample is kept. When execution ends:

- receipt present: receipt wins (`source: "receipt"`), sample fills dimensions the
  receipt lacks;
- no receipt: the sample is the evidence (`source: "sampled"`), and
  **`memory.events` is read once more at the moment `ActiveState` flips**, because
  systemd keeps the cgroup readable until the unit is garbage collected (verify;
  design risk R1).

An `oom_kill` count in either source classifies the attempt `resource_limit/memory`.
If neither source has the file (cgroup gone, non-Linux), `resource_evidence: "none"` is
stated; absence is never reported as zero.

A spike shorter than a turn can be missed in the sample, but `memory.peak` and
`oom_kill` are kernel high-water marks, so a late read still sees them: the loop need
not be fast, only the *last* read must happen before the cgroup disappears.

## 3. Typed terminal causes

`store._limit_breach` becomes a function of the merged `resources` block and returns
`{cause: "resource_limit", kind, observed, declared, retryable: false}`. Kinds:
`memory` (oom_kill), `tasks` (pids_max_events), `disk` (workspace delta reached the
need's `disk_bytes`, or the worker reports ENOSPC on the workspace). The existing
message is kept as `detail`. `resource_limit` is not an infrastructure retry
(see `docs/infrastructure-retry.md`): the outcome is `failed` with the cause, so a
submitter cannot loop on it. The failure signature (`failure_signature`) of the old
generic outcomes is unchanged for the `none`-evidence case.

## 4. History and the audit

On attempt finalisation (the same transaction that stores the result) insert one row per
dimension present: `memory_peak` (prefer `memory_nonreclaimable_peak_bytes`, fall back to
`memory_peak_bytes`, as `placement._learned_peak` already does), `tasks_peak`,
`cpu_usage_usec / execution_seconds` (mean cores), `disk_delta`. Outcomes recorded:
`succeeded` and `resource_limit` (a killed attempt's peak is a lower bound on need and
is the most informative sample). Infrastructure outcomes are excluded (not evidence).
Percentiles are computed on read from <= 50 rows per series, by one grouped statement per
status request (fan-out: independent of handler count; Benchday rule 14 analogue).

`placement._learned_peak` is refactored to read the same table, so there is one learned
number. Backfill: on first start with the new schema the table is filled from the last
50 attempts per handler in one pass, bounded; the pass reports how many it read.

Audit rule, per handler `h`, dimension `d`, with `n >= min_samples` (default 5):

- `declared_below_observed` if `need[d] < max(observed_max, p95)`
- `admit_below_typical` if `admit[d] < p50`
- `declared_far_above_observed` (info only, never a warning) if `need[d] > 4 x observed_max`

The audit is computed per distinct declared `need` seen in the window (a declaration
changed by hand starts a fresh comparison; old rows are compared to the declaration
they ran under, kept in the row).

## 5. The optional floor

`resource_floor` section, absent = off:

```json
"resource_floor": {"margin": 1.15, "min_samples": 5, "dimensions": ["memory_bytes"],
                    "handlers": ["compile.*"], "history_max_age_seconds": 2592000}
```

A submit (or a handler declaration change) whose `need[d]` is below
`max(observed_max, p95) x margin` is refused with HTTP 422 and
`resource_floor: need.memory_bytes 8.0 GiB < floor 9.4 GiB (observed max 8.17 GB over
12 attempts, latest <attempt id>)`. Rules: unknown keys fail closed; `margin >= 1.0`;
`min_samples >= 3`; patterns are exact handler ids or a trailing `.*`; the floor never
rises above the handler's own `resource_limits` ceiling if one is configured (that case
is refused as `floor_exceeds_ceiling`, named, for the operator). A history read error
disables the floor for that request and surfaces `resource_floor_unavailable` in status;
it never refuses with a made-up number and never silently admits when `strict: true`.
`strict` (default false): when true, an unreadable history refuses.

Why optional: a floor derived from history can lock a legitimately smaller job out
(a small input to a large handler). It is therefore per-handler opt-in, and the owner
decides (open question 1).

## 6. Metric definitions

`metrics_schema.py` holds entries like:

```
name: docker_cache.seconds     unit: s
measures: wall seconds spent in cache begin + prune + finish phases
excludes: the attempt's own execution (see docker_cache.session_seconds)
scope: attempt   source: worker
```

A result's `metrics` keys are validated against the schema when stored. Unknown keys
are dropped from status/roster and counted in `metrics_undeclared_total` with the first
handful of names shown in status, so a typo is visible, not lost. Handler-defined
metrics (Benchday's `postgresReady`) are declared by the handler's own registry release
manifest, `metrics: [{name, unit, measures, excludes}]`; a handler that reports a name
its manifest does not define gets the same treatment.

## 7. Positive controls (instrument verification)

Each instrument has a test that makes the quantity move by a known amount and fails on
the unfixed code:

- memory: a real attempt (or a cgroup-v2 test harness where available) allocating
  1 GiB reads `memory_peak` within +-15 % and `source` correct; skipped with an
  explicit `skip` reason (never a pass) where cgroups v2 are not delegable.
- oom: a unit with `MemoryMax` 64 MiB that allocates 256 MiB yields
  `resource_limit/memory` even when the wrapper is SIGKILLed before the receipt.
  Fails on `origin/main` (generic `stopped without a result`).
- disk: write 200 MiB into the workspace, delta within +-10 %.
- cpu: a busy loop for 2 s reads ~1 core; an idle sleep reads ~0.
- history/audit: synthetic history makes the audit fire at p95 and not below
  `min_samples`; the floor refuses at exactly `observed x margin` and admits just above.
- metrics: a metric defined as "cache only" fed a whole-attempt value in a fake handler
  is caught by a schema test that cross-checks against `session_seconds` (the sum of
  declared sub-phases must not exceed the whole).

A "not measured" reading is distinguishable from zero in every control.

## 8. Risks and open points

- R1: whether the cgroup remains readable after the unit enters `failed`. The unit's
  cgroup is removed when the unit is released; if the read loses the race the last
  loop sample is used. Task 1.1 measures this on a real kernel before anything is built
  on it.
- R2: PSI `full` reads 0 on this kernel; none of this depends on PSI.
- R3: history growth: bounded per (handler, dimension) and by age.
- R4: memory.peak counts page cache; `memory_nonreclaimable_peak_bytes` is preferred
  for the audit of memory, with `memory_peak_bytes` shown beside it.

## 9. Open questions for the owner

1. Floor default: warning only (recommended), or refuse on submit for named handlers?
2. Who edits the declaration when flagged: leave to humans (recommended), or let the
   authority suggest the new number in the warning text (proposed, no auto-apply)?
3. Is `resource_limit` non-retryable acceptable, given an operator may want one
   automatic retry at a higher need? (Recommended: no automatic raise; names the number.)
