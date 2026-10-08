## Why

Overnight 2026-10-07/08 the fleet failed in ways it could not name.

- **An OOM kill read as a lost lease.** The arm64 daemon build hit its 8 GiB cgroup limit (`MemoryMax`, `OOMPolicy=kill`
  in `supervision.py`) and the kernel killed the whole unit, wrapper included. The wrapper is the only writer of the
  receipt that carries `resources.oom_kill`, so no receipt existed. The worker loop saw the dead unit and either the lease
  keeper's liveness predicate returned false (`_lose('WorkNotAlive')`, logged as "lease lost") or the poll loop raised
  `execution stopped without a result`. The attempt ended `infrastructure` with `error: WorkloadError`, detail
  `execution lease lost`. `_limit_breach` in `store.py` would have said "raise the job's need, a retry would hit the same cap",
  but it only fires when the receipt survives. The result: the build was retried into the same wall.
  The worker already reads systemd's `Result` property in `supervision.py` `_inspect_manager`, and `oom-kill` is one of
  its values. Nothing uses it.
- **A job sat unclaimed 17+ minutes with the reason in a log line.** `placement.place` computes, per worker, a precise
  rejection (`insufficient host memory: claim 8.0 GiB > free ...`, `required capability absent`, `worker holds an active
  attempt or cleanup`, `avoiding <worker>: same failure signature`), then writes it into `jobs.reason` as free text that is
  overwritten every placement round (and is sometimes a JSON string of the `rejected` list, truncated to 8192 characters).
  A reader cannot tell "no worker advertises the handler" from "the one worker is busy" without parsing prose, and cannot
  tell how long it has been that way.
- **Attempts ended `infrastructure` for causes buried in logs.** `outcome: infrastructure` is one value for OOM, lease
  loss, worker restart, deadline, cancel and environment integrity failure. Callers (ZZOPS) choose retry vs stop from
  it, so every cause is retried.

**Design records realised:** `_plans/durable-workloads.md` ("2026-10-02: a breach of the job's OWN limit ends the job, it
does not retry"; "Measured host memory and learned claims"). **Stale in it:** the breach rule is stated as applying when the
receipt shows `oom_kill`; after this change it applies when the kernel or systemd says so, receipt or not.

## What Changes

1. **A closed vocabulary of terminal causes.** Every terminal job result carries `cause: {kind, retry, evidence}` with
   `kind` in a fixed enum (see design §1) and `retry` in `same | elsewhere | after_change | no`. `unknown` exists as an
   explicit value with `evidence.unreadable` listing what could not be read; it is never defaulted to infrastructure.
2. **Causes are derived from kernel and systemd evidence, not inferred from absence.** The worker samples the attempt
   cgroup's `memory.events` (`oom_kill`, `oom`, `max`) and `pids.events` each turn, keeps the last values, and when the
   unit ends without a receipt reads systemd `Result`, `ExecMainStatus` and the last sample. `oom_kill>0` or `Result=oom-kill`
   yields `oom_killed` with `retry: after_change`. The lease keeper's `WorkNotAlive` no longer masks the cause.
3. **Structured placement blockers.** A queued job's view carries `placement: {since, evaluated, blockers[]}` where each
   blocker is `{worker, host, code, detail}` with `code` from a closed enum; rewritten only when its content changes, so
   `since` is stable and age is computable.
4. **Opt-in stall and queue-wait deadlines.** A handler MAY declare `progress_deadline_seconds`: a running attempt whose
   reported progress does not change for that long ends with cause `stalled_no_progress`. A submission MAY declare
   `max_queue_seconds`: a job with no attempt for that long ends `expired` with cause `unplaceable` and its last blockers.
5. `reason` stays as a human line derived from `cause`/`placement` for existing readers.

## Capabilities

New capability `outcome-causes`.

## Impact

`workloads/model.py` (cause enum, validators, `Limits`), `workloads/worker.py` (sampling, classification at the two
raise sites and the lease loss path), `workloads/supervision.py` (`inspect` already returns `Result`; add
`ExecMainStatus`), `workloads/resource_usage.py`, `workloads/store.py` (`complete`/`_abandon`/`_expire` attach `cause`;
`_job` returns it), `workloads/placement.py` (blockers), `workloads/schema.sql` (`jobs.placement`, `jobs.cause`).
Additive on the wire. Companion: ZZOPS change `progress-deadlines-and-named-causes`. Related: Livestack change
`work-scopes-and-cascade-cancel` introduces cause `scope_closed`.
