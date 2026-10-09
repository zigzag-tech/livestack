# Live environment rollout observation — 2026-10-09

Read-only workload rollout status at 16:50 UTC, plus authenticated job and
capability readbacks. No rollout spec, worker configuration, or authority
policy was changed; no full-test or publishing handler was invoked.

The authority reports deployment unit unit-f38a7baa and rollout spec generation
2 in mode observe. The reconciliation result says “observe mode: nothing was
changed.” The zz-joe-e2e worker set uses canary auto and requires at least two
claiming workers; its current report says
canary_not_representative:benchday.e2e.task.v1. The installed
benchday.e2e.task.v1 handler is reported on zz-joe-e2e-2 only; workers 1, 3,
4, and 5 lack it. Worker 1 is behind; the other worker rollout states are
unknown. The authenticated capability endpoint allows Flutter, Rust, and
task-E2E handlers and forbids full-E2E, dependency, commerce, and
release/publishing handlers.

The current-source Rust job bbbe018da9c2400c9a1774d39028feeb remains queued
without an attempt. Its placement report names the Rust-profile worker busy,
workers 1, 3, and 4 without that environment profile, and worker 5 draining.
The rollout observation does not establish additional eligible capacity or a
completed worker/SDK rollout.

The configured task caller can read job and rollout status but is not an
authority admin: GET /v1/workloads/status returned 403,
“authority status requires an admin principal.” No other credentials were used. Consequently, the installed decision-ledger summary remains
unverified through this caller.

Keep automatic environment selection disabled. Full/coalesced E2E and
publishing remain with their existing coordinators. This evidence does not
certify a full suite or a release.

## Current profile and authority readback — 2026-10-09 18:31 UTC

Read-only authority state reports worker 1 and worker 2 ready with Flutter,
Rust, and task-E2E profiles. Worker 1 was drained at generation 5, updated on
release 377bb4e4, and re-enabled at generation 7. The observe report remains
at spec generation 2 with no waiting reason. Automatic selection remains
disabled. The authority process is active on release d55c4c13; the exact
source-bundle match and deployed dependency difference are recorded in
rollout-recheck-20261009.md. No rollout promotion or rollback was performed.
