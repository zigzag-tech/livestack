# Wall-clock bounds on shared capacity

Nothing may hold shared capacity indefinitely. Two bounds, both enforced by the authority's sweep
(`WorkloadStore._wall_clock_verdict`, called from `_job_verdict` on every `_expire`/`sweep`):

| Bound | Default | Enforcer outcome |
|---|---|---|
| `limits.max_attempt_seconds` | 10800 (3 h) | running attempt fenced (`cleanup`, worker held until clean); job `failed`, cause `attempt_wall_clock_exceeded`, retry `after_change` |
| `limits.max_queue_age_seconds` | 86400 (24 h) | queued job `expired`, cause `queue_age_exceeded`, retry `after_change` (counts jobs that already had attempts) |

Both are read at authority start (restart-only, like every limit), accept [30, 86400] and cannot be disabled.
The clock is per attempt: an infrastructure retry starts its own 3 h. The meaning of a breach is the same
every time: the job is too large or stuck, so the OWNER splits it. It is never retried unchanged.

## Lowering, and the only way to raise

A schema-4 submission may carry `max_attempt_seconds` (30 to the handler's bound); a larger value is refused.
Raising is an operator decision recorded in `authority.json`, one named handler at a time, with a reason:

    "limits": {"wall_clock_exceptions": {
        "some.handler.v1": {"max_attempt_seconds": 21600, "reason": "single indivisible step; split planned in <ticket>"}}}

The set is empty by default. An exception's bound must exceed the global one and stay at most 86400.

## Seeing it coming

- `GET capabilities` -> `wall_clock {max_attempt_seconds, max_queue_age_seconds, exceptions}`.
- A running job's view carries `wall_clock {limit_seconds, source (global|exception|submitter), elapsed_seconds, remaining_seconds}`.
- The roster adds `running[].wall_clock_limit_s` and the warnings `attempt_near_wall_clock_bound` and
  `queue_near_age_bound` at 80% of the bound, so an owner can split work before it is cut.

## Measurement behind the defaults (2026-10-17, authority ledger, 734 attempts over 77 h)

Longest attempt per handler (hours): e2e.full 3.02 (one, ended `download duration budget exhausted`; p95 2.24),
rust-check 1.01, e2e.task 0.94, image 0.50, everything else under 0.2. Longest queue wait before the first
attempt: flutter-check 2.37 h, image 1.39, e2e.full 1.28, release.* 1.21; attune.produce_item 0.36 h.
The ledger keeps terminal jobs 6-24 h (retention tiers), so older history does not exist to measure.
`benchday.e2e.full.v1` runs close to the attempt bound: its owner should keep splitting full E2E rather than ask for an exception.
