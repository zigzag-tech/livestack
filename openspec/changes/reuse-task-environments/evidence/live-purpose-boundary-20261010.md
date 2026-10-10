# Live environment-purpose boundary and rollout readback

Checked 2026-10-10 at 19:21 UTC through the configured workload client and
read-only worker/rollout status commands.

## Installed authority refusal

The authenticated `GET /capabilities` response reports environment policy
version 1. It allows Flutter compilation, Rust compilation, and exact task-E2E
handlers. It classifies full E2E, dependency, commerce, and all release/publish
handlers as forbidden.

To verify the authority itself, sent valid schema-3 environment-bound jobs
directly to `POST /jobs` for `benchday.e2e.full.v1` and
`benchday.release.app.android.v1`, bypassing SDK preflight. Both were refused
with HTTP 403 `environment_scope_forbidden`. An authenticated `GET /jobs`
confirmed neither request created a job. This did not run a test or publish a
release.

## Current rollout state

`rollout status` still reports mode `observe` for unit `unit-f38a7baa`; the
reconciler says it made no changes. Its last report labels worker 1 `behind`
and workers 2–5 `unknown`. The set requires two workers claiming work.

The live roster has the task-E2E handler only on workers 1 and 2. Worker 1 is
running a Rust compilation job; worker 2 is running the ZZOPS full-suite job.
Workers 3 and 4 are idle but do not have the task-E2E handler; worker 5 is
disabled for claims. A separate selected task-E2E request is queued behind the
active attempts. No authority or worker was drained, restarted, or reconfigured
for this readback.

## Local acceptance checks

Four focused tests passed on `lappy-bellinzona` at 19:25 UTC in 2.97 seconds: profile-probe
invalidation after toolchain replacement, full-test behavior after environment
rollback, installed purpose policy, and scope-close cancellation of running and
queued descendants. The cancellation test uses systemd `PrivateTmp`, so its
pytest base directory was placed under `~/.cache` rather than `/tmp`. Its fake
quota report also now reads the rounded per-environment limit from the persisted
marker; the previous fixed 32 GiB value disagreed with this fixture's 8 GiB
aggregate cap. This is fixture evidence, not admitted compiler/ABI or
installed-handler cancellation evidence.

## Remaining acceptance

The live purpose boundary is confirmed. Authority and worker release
provenance, a representative post-rollout canary, rollback readback, and
consumer SDK/default activation remain open. Automatic selection stays off
until those checks pass.
