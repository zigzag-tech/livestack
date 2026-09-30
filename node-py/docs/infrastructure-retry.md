# Infrastructure retry: two attempts, and not back onto the worker that just failed

`Limits.attempts` defaults to **2** (was 3). An attempt that ends with outcome
`infrastructure` (worker-declared, or an expired lease) requeues the job once;
the second infrastructure outcome is terminal (`failed`). Product failures never
retry. `Limits` still refuses more than 3. Measured 2026-09-30: three attempts on
a starved worker burned 41-99 minutes of wall for nothing.

The production authority's `authority.json` sets only `terminal_seconds` under
`limits`, so it takes the code default: a restart of `livestack-workload-authority`
is what makes the new limit live. An explicit `limits.attempts` in the config
would override it.

## Worker avoidance

A requeued job is not placed again on a worker whose earlier attempt of the SAME
job ended `infrastructure`, while any OTHER worker could ever run it. "Could ever
run it" means: fresh (seen within `fresh_seconds`), advertises the handler,
matches the selector labels, and its declared capacity covers the job's `admit`
vector. Momentary load, busy state and readiness are ignored, so the job waits
for a busy worker rather than returning to the failed one. If no such other
worker exists the same worker is used (a job is never stranded).

- Refusal reason (visible in `job.reason`, rule 13):
  `avoiding <worker>: same failure signature <sig> on attempt <n>`.
- Expiry: `AVOID_SECONDS = 1800` after the requeue (`jobs.updated`). A lone or
  recovered worker is used again after that. A deadline ends the job sooner.
- Signature (`model.failure_signature`, exposed on the job view as
  `failure_signature`): `<tag>-<hash8>`. Tag is the worker's `result.error` or
  `exit<code>`; the hash covers error, first line of `detail`, and exit code with
  digits collapsed to `#`. At most 41 characters, stable across pids/timestamps.
  Placement avoids the worker of every infrastructure-ended attempt; the
  signature names why.

### Across a job boundary

A caller that creates a NEW job after one failed (Benchday's cargo retry) passes
the hint as two job labels: `harmony.avoid.worker` (one worker id) and
`harmony.avoid.signature` (one signature). Same rule, expiring `AVOID_SECONDS`
after the new job's `created`. Labels are already bounded (64 keys, 256 chars).
The failed job's view gives both values: `attempts[-1].worker` and
`failure_signature`.

Tests: `tests/test_workload_store.py` (`test_infrastructure_retry_*`,
`test_avoidance_expires_*`, `test_avoid_labels_*`, `test_only_infrastructure_retries_and_two_attempt_limit`).
