## Why

A handler's declared resource needs and the metrics its workers report are both
claims. Nothing in Livestack compares them with what attempts actually did, and the
reported numbers carry no definition. Three incidents in one week:

1. **A declared memory cap below the real peak.** Benchday's
   `compile.daemon.linux.arm64` declared `memory_bytes` 8 GiB; the arm64 build peaked
   above 8.17 GB (the x64 build peaks about 5.8 GB), so the kernel OOM-killed rustc.
   The operator saw `execution lease lost` and `execution stopped without a result`,
   not "out of memory". The fix was a hand edit to 16 GiB in Benchday's
   `contracts/release-executors.json`. Nothing would have flagged the next handler
   declared too low.
2. **A number that measured something else.** `docker_cache.seconds` was the whole
   attempt (about 95 % of wall time), not the cache cost, and misled planning for
   hours. (The Livestack side is fixed on `agent/docker-cache-overhead`:
   `seconds` is now the cache phases only and `session_seconds` the whole attempt.
   The defect class is not fixed: nothing stops the next metric doing the same.)
   Benchday's `postgresReady` is really "first run milestone" (about 150 s, mostly
   dependency install), and `startupBudget` was sized from it.
3. **Memory data exists, in places that cannot help.** `resource_usage.py` already
   records `memory_peak_bytes`, `oom_kill`, `pids_max_events`, `tasks_peak` and
   `cpu_usage_usec` from the attempt cgroup, and the worker samples a
   non-reclaimable peak. Placement reads them (`placement._learned_peak`,
   `openspec/changes/host-memory-ledger`) to charge a *claim*. But: the receipt is
   read only when the wrapper exits cleanly, so an OOM that takes the supervisor
   down leaves no receipt and a generic infrastructure failure; `_limit_breach`
   (`store.py`) types only receipts that exist; there is no disk or CPU history; and
   nothing tells an operator or a submitter that a declaration is below what the
   handler is observed to need.

**Design records realised:** `_plans/durable-workloads.md` (handler `need` / `admit`),
`openspec/changes/host-memory-ledger` (learned peaks). **Stale in them:**
`host-memory-ledger` learns peaks only to *charge* placement; it never tells anyone
the declaration is wrong, and it learns memory only.

## What Changes

1. **Per-attempt measured usage survives a kill.** The worker samples the attempt
   cgroup (`memory.peak`, `memory.events` `oom_kill`, `pids.events`, `cpu.stat`,
   plus a workspace disk delta) during the attempt loop, not only in the exit
   receipt, and merges the last good sample into the attempt result when execution
   ends without a receipt. A result built from a sample says so
   (`resources.source: "sampled"` vs `"receipt"`).
2. **Typed terminal causes.** An attempt that ended with `oom_kill > 0` or
   `pids_max_events > 0` in the receipt *or the last sample* completes with the
   outcome cause `resource_limit` (kinds `memory`, `tasks`, `disk`), names the
   dimension, the observed peak and the declared need, and is **not retried
   unchanged** (a retry hits the same cap). `execution lease lost` / `stopped without
   a result` remain only when no resource evidence exists, and then say so
   (`resource_evidence: "none"`).
3. **Per-handler resource history in the authority.** For every handler (and job kind
   where the payload names one), the authority keeps a bounded rolling window of the
   last N succeeded **and** resource-limited attempts' peaks per dimension, and
   serves p50 / p95 / max with sample counts.
4. **Declaration audit.** A handler whose declared `need` (the enforced cap) is below
   the observed p95 or any observed peak, or whose declared `admit` is below the
   observed p50, is flagged `declared_below_observed` in the handler status and the
   roster, with the figures. This is a warning only by default.
5. **Optional admission floor.** A schema-validated authority policy section
   `resource_floor` (absent = off) may enable, per dimension, refusing a *submit*
   whose `need` is below `max(observed peak, p95) x margin` with a named refusal
   that states the floor and its evidence. Fail closed: a malformed section
   refuses startup; insufficient history (fewer than `min_samples`) never raises
   a floor; unreadable history disables the floor and says so.
6. **Metrics have definitions.** Every timing or number a handler or worker puts in a
   result's `metrics` is declared in a metrics schema (name, unit, what it measures,
   what it excludes, scope). Undeclared metric names are dropped from status
   surfaces and counted, never silently accepted. Every new instrument ships a
   positive-control test that makes the measured quantity move by a known amount.
7. **A "measure the instrument" checklist** in `node-py/docs/measured-resources.md`.

**Not changing:** the placement claim arithmetic of `host-memory-ledger` (this change
only *reads* the same history through a shared helper); cgroup limit enforcement;
who sets a handler's `need` (owner decision below).

## Impact

- New: `workloads/resource_history.py`, `workloads/metrics_schema.py`,
  `node-py/docs/measured-resources.md`.
- Modified: `worker.py` (sample loop, typed cause), `resource_usage.py` (disk delta,
  `source`), `store.py` (history, status, `_limit_breach`), `placement.py`
  (shares `_learned_peak`), `roster.py`, `config.py` (`resource_floor`), `http.py`
  (status).
- Rollout: authority first (accepts new result keys), then workers. An old worker
  keeps sending receipt-only results; they are history samples with
  `source: "receipt"` and are still flagged.
- Benchday: follow-ups in `openspec/changes/measured-resource-declarations/benchday-followups.md`
  (metric renames, declarations, no code needed for the warning).
