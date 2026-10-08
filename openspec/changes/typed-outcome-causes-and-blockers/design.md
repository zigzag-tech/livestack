## 1. The vocabulary

`cause.kind` (closed enum, validated in `model.py`; unknown kinds from a newer worker are stored as `unknown` with the
original name in `evidence.reported_kind`, bounded to 40 chars, so an old authority never rejects a result):

| kind | meaning | evidence source | retry |
|---|---|---|---|
| `succeeded` | exit 0 | receipt | n/a |
| `handler_failed` | handler exit non-zero, no limit breach (today's `product_failure`) | receipt | `no` |
| `oom_killed` | memory limit reached | `memory.events oom_kill`, or systemd `Result=oom-kill` | `after_change` (more memory or smaller work) |
| `pids_exhausted` | task limit reached | `pids.events max`, receipt | `after_change` |
| `wall_time_exceeded` | handler `max_seconds` | wrapper | `after_change` |
| `disk_exhausted` | ENOSPC in workspace, or filesystem reserve breach | receipt / statvfs at stop | `elsewhere` |
| `lease_lost` | authority refused or lease deadline passed while the unit was alive | lease keeper `error` | `elsewhere` |
| `worker_lost` | attempt lease expired with no word from the worker | authority `_abandon` | `elsewhere` |
| `capability_absent` | worker cannot run the job (handler/release/capability missing at claim or start) | worker preflight | `elsewhere` |
| `resource_exhausted` | host admission refused for the whole deadline (placement) | blockers | `elsewhere` |
| `unplaceable` | queue-wait deadline with no eligible worker | last `placement` | `after_change` |
| `stalled_no_progress` | progress deadline | heartbeat progress | `elsewhere` |
| `deadline_expired` | absolute `deadline` passed | authority | `no` |
| `scope_closed` / `cancelled_by_owner` | someone ended it on purpose | authority | `no` |
| `unknown` | stopped, cause unreadable | `evidence.unreadable` | `elsewhere` once, then `no` |

`retry` is advice from the authority, which sees every attempt; callers decide policy but no longer guess. `elsewhere`
adds the existing `harmony.avoid.worker`/`signature` label behaviour; `after_change` means the same inputs on any worker
are expected to fail the same way (the existing "a retry would hit the same cap" rule, now applied to every kind).

`outcome` is unchanged (`succeeded | product_failure | infrastructure`) so every existing consumer keeps working. `cause`
refines it. Invariant: `outcome == infrastructure` implies `cause.kind != handler_failed`; `product_failure` implies
`handler_failed`.

## 2. Where the cause is derived (worker)

Process owner: the **worker** owns kernel evidence (only it can read the attempt cgroup); the **authority** owns the stored
cause and the causes only it can know (`worker_lost`, `deadline_expired`, `scope_closed`, `unplaceable`).

The poll loop already samples `cgroup_nonreclaimable(attempt_cgroup)` every ~0.2 s and `inspect(attempt)` returns systemd
properties. Add to that sample, same cadence and the same cgroup path, one bounded read (4 KiB cap, as `resource_usage.values`
does) of `memory.events` and `pids.events`, kept as `last_events` in the attempt's local state. On every exit path
(receipt, `execution stopped without a result`, lease loss, any exception) `classify_stop(last_events, systemd_show,
receipt, lease_error)` runs once, in this precedence:

1. receipt present: today's `_completion_from_exit` plus `cause` from `resources` (`oom_kill`, `pids_max_events`).
2. `last_events.oom_kill > 0` or `systemd Result == 'oom-kill'` -> `oom_killed`, evidence `{source, memory_peak_bytes (last sample), limit: need.memory_bytes}`.
3. `last_events.pids max > 0` -> `pids_exhausted`.
4. `lease.error` starts with `LeaseExpired`/refusal and unit alive at the time -> `lease_lost` with the keeper's error.
5. unit dead, no receipt, none of the above readable -> `unknown` with `evidence.unreadable` naming each file that failed to read
   (ENOENT after cgroup removal is recorded, not hidden) and the systemd `Result`/`ExecMainStatus` that were readable.

The lease keeper's `WorkNotAlive` is demoted from a lost-lease reason to a trigger: it still stops the attempt, but the
completion cause comes from `classify_stop`, not from the keeper's string. This is the defect behind "execution lease lost".

Race honesty: a kill and cgroup removal inside one 0.2 s turn can lose `memory.events`; a failed transient unit normally
stays loaded until `_stop` runs `reset-failed` (`supervision.py`), so `Result=oom-kill` is usually still readable and rule 2 has a second source.
`supervision.py` also notes a unit can be collected between inspect and stop, so the second source can be absent too (task 2.2 measures how often on a real host). Both sources
are named in `evidence.source`, and a stop with neither readable is `unknown`, never `oom_killed` by guess.

Windows and macOS: `DarwinLimits.oom_kill` and `WindowsLimits.oom_kill` are already receipt fields; rule 1 covers them. No new
sampling there. `lease_lost`/`unknown` apply unchanged.

## 3. Where the cause is stored (authority)

`jobs.cause TEXT NULL` (JSON <= 2 KiB, enforced by `encode(..., 2048)` and a CHECK on length) set in the same transaction
that writes `jobs.result`: `complete` (from `completion.cause`, validated), `_abandon`, `_expire`, `cancel`, scope cascade.
A terminal job without a cause is a defect: `_terminal_result` takes the cause as a required argument. Existing rows
migrate with `cause = null` and views report `cause: null, cause_reason: "predates_causes"`, not `unknown`.

`_limit_breach` becomes a rendering of `cause` (`oom_killed`/`pids_exhausted`) so the human `reason` and the typed field cannot disagree.

## 4. Placement blockers

`place()` already builds `rejected = [{worker, reason}]`. Change `reason` to `{code, detail}`:

`worker_stale | worker_draining | worker_not_ready | handler_not_advertised | release_incompatible | capability_absent |
memory_insufficient | host_pressure | principal_cap | avoiding_failure_signature | worker_busy | environment_busy |
environment_affinity_wait | compilation_refused | deadline_unfit | no_workers`.

`jobs.placement TEXT NULL` JSON `{since, evaluated, blockers: [{worker, host, code, detail}], truncated}`: at most 16
blockers, `detail` <= 160 chars, document <= 4 KiB. It is written only when the digest of `blockers` differs from the stored
one (so a steady wait is not a write per claim poll) and `evaluated` is updated at most once a minute. `since` is the
time the current digest first appeared. Cleared when the job is claimed. Never-evaluated is `placement: null`; evaluated with
no workers is `blockers: [{code: no_workers}]`. `reason` is derived from the top blocker for compatibility.

## 5. Opt-in deadlines

- `handler.progress_deadline_seconds` (worker handler config, default none, 30..86400): the worker's heartbeat already carries
  `progress`. The authority records `attempts.progress_changed` when the progress document's digest changes; `_expire` abandons a
  running attempt whose `now - progress_changed > deadline` with cause `stalled_no_progress`. A handler that does not report
  progress cannot use it (validated at handler load: declaring a deadline without `reports_progress: true` is refused).
- Submission `max_queue_seconds` (30..86400, default none): `_expire` expires a queued job with zero attempts past it, cause
  `unplaceable`, evidence = the last `placement`. This is distinct from absolute `deadline`, which callers must compute and which
  the existing estimate-fit rule already enforces for the fit case.

Both are off unless asked for: Livestack imposes no policy on handlers. ZZOPS supplies the policy (its change).

## 6. Bounds

`cause` 2 KiB, `placement` 4 KiB, per job row, so storage is bounded by the existing job-count bounds
(`active_jobs`, `terminal_jobs`). Samples are 2 x 4 KiB reads per 0.2 s turn per running attempt; `limits.attempts` and
`claims_per_worker` already bound attempts per worker. No new unbounded table.

## 7. Failure and observability

- Classification never raises: any exception inside `classify_stop` yields `unknown` with `evidence.classifier_error`.
- Worker logs one line per terminal attempt: `attempt <id> ended cause=<kind> retry=<advice> source=<..>`.
- The decision ledger's `completion_record` gains `cause_kind` (per `_plans/decision-ledger.md`: every placement decision
  leaves a record; its completion now says why it ended).
- A cause kind the authority had to coerce to `unknown` increments a counter in `status()` (`causes_coerced`).

## 8. Tests that cannot be faked

- Real cgroup (Linux CI host with systemd user manager): a handler that allocates past `MemoryMax` ends `oom_killed`
  with `evidence.source` set. Skipped with an explicit `skip` reason where cgroup v2 delegation is unavailable, and a
  separate always-run test feeds `classify_stop` recorded `memory.events` / `systemctl show` fixtures captured from the real run.
- Positive control: the same scenario on the old worker yields `error: WorkloadError`, detail `execution lease lost` (kept as a regression
  fixture to prove the test detects the defect).
- Placement: table-driven, one test per blocker code, and "steady wait performs no write" asserted by counting UPDATEs.
