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

## Evidence: what the memory ledger charged on zz-joe, 2026-10-11

Waiting job: `benchday.release.hub.v1` (need 4 GiB), refused `claim 4.0 > free 0.6 (available
37.5, reserve 1.0, running attempts 23.1, model servers 12.8)`. Real `free`: 40.7 GiB
available, 23.5 used. The ledger charges `MemAvailable` (measured) minus each running
attempt's UNREALISED claim (`claim - current`), minus the largest model-server load
transient (peak - current, unless all units are resident). It never subtracted declared
`need` for handlers with a measured peak. Attempt cgroups sampled at the same time: the
five new attempts were 21 s old and held 0.1-0.24 GiB each, so nearly their whole claim
was still "to come".

Measured (authority `resource_history`, succeeded attempts, last 50; GiB):

| handler | need | stated admit | measured non-reclaimable max / p95 / median | claim before | claim after |
|---|---|---|---|---|---|
| release.cli.linux | 3 | none | 1.32 / 1.31 / 1.25 (24) | 3.00 | 1.65 |
| release.hub | 4 | none | 1.98 / 1.97 / 1.94 (50) | 4.00 | 2.48 |
| release.web.linux | 4 | none | 1.92 / 1.92 / 1.88 (33) | 4.00 | 2.40 |
| release.daemon.linux (x64, need 8) | 8 | none | 8.81 / 8.79 / 4.63 (50) | 8.00 | 8.00 (cap) |
| release.app.android | 9 | none | 8.70 / 8.46 / 7.39 (50) | 9.00 | 9.00 (cap) |
| release.daemon.macos | 12 | none | 9.38 (8) | 12.00 | 12.00 (<5 per need) |
| e2e.full | 10 | 4 | 4.76 / 4.65 / 1.75 (50) | 4.76 | 4.76 (stated admit) |
| e2e.task | 8 | none | 4.46 / 4.46 / 2.80 (19) | 8.00 | 5.58 |
| compilation.image | 10 | 4 | 4.47 / 4.12 / 3.83 (28) | 4.47 | 4.47 |

Not lowered: every `need` (cgroup cap; cache-inclusive peaks sit at the cap), the arm64 and
Apple daemon figures (16 / 12 GiB), declarations of handlers that state an `admit`.
Model servers: `harmony-image` was not resident, so peak 16.3 - current 3.5 = 12.8 GiB was
reserved for a possible reload; that is the design's load-transient term and is unchanged.

## Cores: the same rule, with a measured PEAK (2026-10-11, follow-up)

The CPU reservation was purely declared: a job that states no cpu `admit` reserves its `need`
(the cgroup quota), so on zz-joe four running attempts reserved 8-10 cores while each used
about 1-1.2, and a release build and idle e2e workers waited with `cpu: needs 2, 0 free
(8 observed less 8 reserved by running attempts)`.

`placement._cpu_claim`: no stated cpu `admit` -> the claim is the handler's measured peak cores
(max over the last 20 succeeded attempts that ran under the same cpu `need`) x 1.25, rounded up to
a quarter core, never below 1 core (or `need`), never above `need`, and only once there are 5
such attempts; before that it is `need`. A stated cpu `admit` is charged as stated. The claim is
used for the fit test, the starved-job hold and the reservation, and RUNNING attempts are charged
their handler's current claim (`charged_need`; the stored `attempts.need` still holds the
declaration), so the roster's `blocked_by` arithmetic agrees. The psi_some CPU admission gate is
untouched.

The figure is `cpu_peak_cores`: the busiest 5 s window of the attempt's `cpu.stat usage_usec`,
sampled every worker turn (`resource_usage.CpuPeak`), absent (never 0) for attempts shorter than
5 s and on macOS/Windows workers. It is recorded beside `cpu_cores`, which is `cpu_usage_usec /
execution_seconds`, a MEAN, and is NOT used for claims: cargo and flutter builds alternate
compile bursts with link/IO phases, so a mean understates the cores to leave free.

Peaks were not recorded before this change, so every claim equals `need` until a handler has 5
succeeded attempts with a peak. The only evidence that exists today is the mean (authority
`resource_history`, succeeded attempts, last 50, cores):

| handler | cpu need | stated admit | mean max / p95 / median (n) | note |
|---|---|---|---|---|
| release.hub | 2 | none | 1.13 / 1.12 / 1.05 (50) | claim falls once 5 peaks exist |
| release.web.linux | 2 | none | 1.26 / 1.26 / 1.24 (21) | |
| release.daemon.linux | 2 | none | 1.58 / 1.58 / 1.55 (42) | near the cap: peak will sit at 2 |
| release.cli.linux | 2 | none | 1.91 / 1.91 / 1.88 (24) | quota-bound, claim ~need |
| release.app.android | 4 | none | 3.18 / 3.17 / 3.05 (34) | quota-bound |
| release.daemon.macos / cli.macos | 2 | none | 3.31 / 6.75 (8 / 16) | no cgroup, no peak, never lowered |
| compilation.rust-check | 4 | none | 3.11 / 3.05 / 1.03 (50) | bursty: mean median 1.0, max 3.1 |
| compilation.flutter-check | 4 | none | 2.55 / 2.44 / 1.71 (50) | |
| compilation.image | 2 | 2 | 1.45 / 1.42 / 1.28 (27) | stated admit, untouched |
| e2e.full | 6 | 2 | 1.77 / 1.54 / 0.94 (50) | stated admit, untouched |
| e2e.task | 4 | 2 (since benchday 78dfc2547) | 1.72 / 1.72 / 1.15 (19) | stated admit, untouched |

Not lowered: every `need`, the arm64 and Apple declarations, any handler that states an `admit`.
