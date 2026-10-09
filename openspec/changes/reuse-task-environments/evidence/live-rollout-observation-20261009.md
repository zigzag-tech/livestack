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

## Reattached live readback after coordinator restarts — 2026-10-09 22:31 UTC

Read-only reads through the authorized Livestack caller and rollout principal
show that environment API v1 currently allows Flutter compilation, Rust
compilation, and `benchday.e2e.task.v1`. It forbids dependency preparation,
full E2E, commerce, and release/publishing handlers. Handler-release status is
generation 57 under policy `benchday-task-environments-5cf9c2175`; defaults
are Flutter `4d37a75d…`, Rust `584701d4…`, and task E2E `217f90a8…`.

The live rollout endpoint reports generation 2810, spec generation 2, unit
`unit-f38a7baa`, mode `observe`, and `applied: []`. Its report is fresh and
lists task E2E on workers 1 and 2 only; worker 1 is `behind`, while worker
states 2–5 are `unknown`. The report proposes staging/draining/activating only
worker 1, but every proposed action is `observed_only`; no configuration or
process changed. Worker roster readback confirms workers 1 and 2 advertise the
task handler, workers 3–5 do not, and worker 5 remains claim-disabled. At the
same read, workers 1–4 held running work, including ZZOPS admission/completion
jobs, so this was not an idle window for rollout.

The owner-reported fence `taskenv-authority-rollout-20261008-v3` expired at
11:22 UTC. It was not renewed or reused. No authority/worker restart, handler
activation, rollback, full-suite submission, or publish was performed.
