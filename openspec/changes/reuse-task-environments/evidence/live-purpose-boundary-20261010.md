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

## Selected task-E2E rebuild attempt — 2026-10-10 21:17 UTC

Benchday submitted one explicit check,
`fleet-workload.task-environment-source-and-cache-freshness` (1 of 936),
against task-E2E handle `06318b61f53c4b3ca5cb7dc620b5702f` after its prior
cancelled attempt left it requiring a rebuild. Job
`8151336bd90b41cc9c9126c93981a7b4` tried `zz-joe-e2e-2` and then
`zz-joe-e2e-1`; both attempts stopped before source materialization because
the worker-host storage budget was exhausted. The authority returned an
infrastructure outcome, no result artifact, and generation 15 with zero bytes
in `rebuild_required`. Queue was 88.629 s, source transfer 8.316 s, and
cleanup 0.012 s. The wrapper reported that completion omitted its bounded
result artifact. No assertion ran, so this is not a passing canary. The
installed task-E2E release is still the prior worker release; no authority or
worker rollout was performed.

## Worker storage readback — 2026-10-10 21:25 UTC

Read-only inspection of the shared `zz-joe` environment volume explains the
admission refusal. The 125 GiB filesystem reports 7.9 GiB used and 117 GiB
available. Four parked replicas reserve 124 GiB of project-quota ceilings:
three at 32 GiB and one at 27.94 GiB. Their markers report 7.88 GiB combined
actual use. The reserved ceilings exactly consume the store's effective
`filesystem_bytes - 1 GiB` budget, so a fifth replica cannot receive a quota
even though physical filesystem space remains. All four also share one owner
scope; their reservations leave about 4 GiB under the 128 GiB per-owner limit.
Growing the volume alone would therefore still cap the next same-owner replica
at about 4 GiB. None of the four replicas had
reached its idle or generation expiry; the worker's bounded prune removes only
expired or `rebuild_required` replicas. No saved environment was deleted or
changed. The failed handle remains `rebuild_required`, generation 15, zero
bytes, with no replicas. This is quota-reservation exhaustion, not a full
physical disk. A Livestack change now compacts parked quota reservations while
preserving their files and restores working limits on reuse. Its local store
tests pass; it is not yet landed or deployed, so it has not changed live worker
capacity or produced a live canary result.

## Local control rerun — 2026-10-10 22:05 UTC

The quota compaction source is now on `origin/main` at
`73ec01d90fd8a31fe43f4837a30e09974bcf7d50`; the 21:25 note above predates
that landing. Focused current-source tests passed: the toolchain-replacement
probe and legacy parked-quota compaction/preservation controls passed 2/2 in
6.32 seconds. The compaction fixture verifies four legacy 32 GiB reservations
shrink while saved cache hashes remain intact, a locked replica keeps its old
limit until unlocked, a fifth handle is admitted, and reuse restores the 32 GiB
execution quota.

Three local worker controls also passed 3/3 in 11.48 seconds: running-job
cancellation, scope-close cleanup of running and queued task-environment jobs,
and worker/authority restart followed by stale completion-receipt refusal.
They ran with Python 3.12.14 and pytest 9.1.1, with both the venv and pytest
base under `~/.cache`. The first attempt put the venv under `/tmp`; systemd's
`PrivateTmp` hid its Python executable. Moving the venv into the home cache
fixed the test setup. These results are local fixture/systemd-worker evidence;
they do not verify the quota change on an admitted shared worker or establish
live toolchain/ABI invalidation. No worker or authority was changed, and the
19:21 UTC rollout readback above remains the last live rollout observation.

## ABI invalidation fixture — 2026-10-10 22:08 UTC

Added `test_machine_abi_change_invalidates_cache_before_reuse`. It changes the
reported machine architecture for the second preparation of a parked handle;
the worker fixture returned `toolchain_changed`, rebuilt the environment,
invalidated the cache component, and removed the old cached artifact. The
toolchain-replacement probe, architecture-change fixture, and quota
preservation fixture passed together (3 passed in 3.17 seconds, Python
3.12.14, pytest 9.1.1). This strengthens local compatibility coverage only;
it does not replace an admitted worker run with a real compiler/ABI change.
