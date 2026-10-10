# Live rollout recheck — 2026-10-10 04:17 UTC

## Authority and retained environments

Read-only authenticated `WorkloadClient` calls against the configured authority
report schema versions `[1, 2, 3, 4]` and environment policy version `1`.
Flutter compilation, Rust compilation, and `benchday.e2e.task.v1` are allowed.
Full E2E, dependency preparation, commerce, and all `benchday.release.*`
handlers are forbidden.

Two existing environments remain parked on `zz-joe`:

| Profile | Generation | Retained bytes | Last environment outcome |
|---|---:|---:|---|
| Rust development | 18 | 18,433,638,400 | reused |
| Flutter development | 13 | 1,245,880,320 | reused |

The authority roster currently reports task-E2E handlers on `zz-joe-e2e-1` and
`zz-joe-e2e-2`; compilation handlers are present on workers 1–4, while worker
5 is draining. This is current capability/roster evidence, not proof that every
worker has every retained-environment profile.

## Release and drain

The user authority service is active on release `livestack-d55c4c13`; its
release selector has not moved. Candidate `livestack-486cf6e5` and the
schema-validated, mode-0600 capacity candidate remain staged and inactive.
The current ZZOPS deploy fence is open.

At this read, three unrelated ZZOPS trains were dispatched and nonterminal:

- `tt_c0dbbb61-a4c5-4a15-9f10-2a52e8b90475`: two client-performance
  assertions on `zz-joe-e2e-1`, about 29 minutes elapsed, no progress sample.
- `tt_269f91e1-ef86-4dfb-9b89-7155a194c04f`: one streams assertion on
  `zz-joe-e2e-3`, about 6 minutes elapsed, no progress sample.
- `tt_bf9109d6-16b8-4dcf-bff2-d997461c0605`: full-suite completion on
  `zz-joe-e2e-4`, recently started, no progress sample.

The previous 10-minute abort-mode drains ended at 01:25Z (task environment
rollout) and 03:59Z (another task's CAS refresh); both released their holds.
No new hold, cancellation, authority restart, or configuration activation was
made while the three trains remained active.

## Acceptance checks

- Fresh `zzops train status --change 48c6d99f32a5ec93a87b6375953bec2fa93fed43`
  reports `complete=true`, `full=false`, PASS for all four changed
  task-environment assertions. The separate `976f…` merge also remains PASS
  for its runtime-freshness assertion.
- Livestack `test_task_environments.py` and `test_workload_environments.py`:
  55 passed.
- Livestack `test_workload_launch_receipt_files.py`: 9 passed, covering bounded
  receipt history, unsafe entries, unknown classes, and oversized receipts.
- Benchday retained-environment integration tests: 9 passed when run with the
  harness-equivalent expanded pinned runtime; handler-release checks: 3 passed;
  Rust wrapper unmanaged-refusal control: PASS. The integration test expects
  the expanded runtime view because its package reads sibling `schema.sql` by
  filesystem path.
- The systemd-backed Livestack worker test was attempted on this editor host,
  where `systemd-run --user` could not start the attempt. This is not a worker
  acceptance result; the existing real `zz-joe` worker evidence remains the
  relevant evidence.

## Remaining work

Complete the admitted source/cache invalidation cases and receipt controls,
then roll the authority/eligible workers/consumer SDK through a successful
abort-mode drain. Verify live quota, cleanup, handler scope, and forbidden
purpose behavior before enabling wrapper defaults. Do not archive either
change until those tasks finish.

## Follow-up — 2026-10-10 04:21 UTC

The one-assertion `streams.sheet-detail-via-hub` cargo reached terminal FAIL
(`sheet detail phases failed: streams.sheet-detail-eligible-machine`). It is
outside this change's `fleet-workload.*` assertion scope and did not alter the
PASS verdicts recorded above. Two trains remain nonterminal: the two-assertion
client-performance admission on `zz-joe-e2e-1` at 33 minutes with no progress
sample, and the full-suite completion on `zz-joe-e2e-4` at 4 minutes with no
progress sample. The fence remains open; no cancellation, new hold, or deploy
was attempted.

## Candidate freshness

Livestack `origin/main` advanced to `24c34f8d` (`Expose bounded workload
outcome manifests`) during this recheck. It changes
`node-py/livestack_node/workloads/http.py` and adds `result_manifest.py`;
staged release `livestack-486cf6e5` predates that source. Do not promote
`486cf6e5` as-is. Refresh the candidate from the current main tree and rerun
`tools/check-authority-release.py` before the fenced deploy.

## Live recheck — 2026-10-10 04:51 UTC

Read-only checks using the configured workload client and ZZOPS human config:

- The authority on `100.64.0.18` is active at
  `/home/ubuntu/.local/share/livestack-workload-releases/livestack-d55c4c13/node-py`.
- `rollout status` remains `mode=observe`, `spec_generation=2`, with
  `applied=[]`. The desired unit is `unit-f38a7baa`, built from `be1d8a42` /
  `be1d8a424684` with release `livestack-be1d8a42`; current Livestack
  `origin/main` is `a435ea20`, so that candidate is stale. The latest report
  says worker 1 is `behind` and workers 2–5 are `unknown`.
- The live roster advertises `benchday.e2e.task.v1` only on workers 1 and 2;
  workers 3–5 lack it, and worker 5 is draining. Worker 1 was running an image
  compilation job, worker 2 was idle, and workers 3 and 4 were running unrelated
  ZZOPS admission/full-completion jobs. The authority's storage report says
  physical storage is `ok` (about 430 GB filesystem free against a 210 GiB
  effective object cap) but does not expose current object-store occupancy.
- The Rust environment remains parked on `zz-joe` at generation 18 with
  18,433,638,400 retained bytes; Flutter remains parked at generation 13 with
  1,245,880,320 bytes. Both last report `reused`.
- The available workload principal can read rollout and environment state, but
  `reload-status` refuses it because that operation requires an admin or
  rollout principal. The active authority was not restarted or reconfigured.
- ZZOPS reports its deploy fence open and three unrelated trains in flight:
  full completion `tt_bf9109d6-16b8-4dcf-bff2-d997461c0605`, streams admission
  `tt_ba541549-48e9-41f1-9fcf-52520deb4815`, and client-performance admission
  `tt_2206d11b-173b-46f7-904d-09d3f44012eb`. No train was cancelled or added.

This recheck made no rollout, config, worker, publish, or test changes. The
authority/worker rollout and its required installed-handler cleanup/forbidden-
purpose readback remain open. Do not activate wrapper defaults or archive until
the candidate is refreshed and the normal fenced rollout/readback succeeds.

## Current-main candidate validation — 2026-10-10 04:59 UTC

Built a local-only authority candidate from exact `origin/main`
`a435ea207a5b7fe0b12df84991e5f008ee4e14c8`, then copied the immutable `_deps`
directory from the active `livestack-d55c4c13` release. The candidate is at
`/tmp/livestack-taskenv-candidate-20261010-0451`. On `lappy-bellinzona`,
`python3 tools/check-authority-release.py` passed all stages: static, boot,
worker registration, and job round-trip.

This replaces the stale source candidate for local validation only. It has not
been copied to the authority host or selected by the live service; no release,
worker, config, fence, or ZZOPS state was changed. Task 4.4 still requires the
normal fenced deployment and live readback after the active trains and stale
worker states permit it.

## Live recheck and current-main candidate — 2026-10-10 09:43:33 UTC

The remote Livestack main tip is now
`92cdbf5ecb9c1e74a7476a4f4f2dfefd48960e4c`. A fresh candidate was assembled
from that exact commit, using only the immutable `_deps` tree from the active
`livestack-d55c4c13` release. The current-main
`tools/check-authority-release.py` passed all four stages: static, boot,
worker registration, and job round-trip. This is local pre-deployment evidence;
the candidate remains in `/tmp` and was not selected by the live service.

The authenticated live read still reports environment policy version 1:
Flutter, Rust, and task-E2E handlers are allowed, while full E2E and all
release/publishing handlers are forbidden. Rust generation 18 and Flutter
generation 13 remain parked with their retained caches. Rollout remains
`mode=observe`, `spec_generation=2`, with `applied=[]`; the selected
`unit-f38a7baa` is still built from `be1d8a42`. The reconciler reports
worker 1 as behind and workers 2–5 as unknown; task-E2E is advertised only by
workers 1 and 2.

One unrelated, non-environment Rust `test daemon` job
`70899c3fee6c4417ad61799bc95562da` was running on `zz-joe-e2e-2` at this
read. Its environment generation is null. Do not restart the authority while
this attempt is live; re-read the job before any fenced deployment. No
authority, worker, config, default-selection, publish, or full-E2E operation
was performed during this recheck.

## Focused current-main source/cache and worker recheck — 2026-10-10 09:56 UTC

Ran six targeted tests against Livestack `origin/main`
`92cdbf5ecb9c1e74a7476a4f4f2dfefd48960e4c` using Python 3.13.15 and pytest
9.1.1 on `lappy-bellinzona`; all six passed. The cases cover incremental
source reconciliation and lockfile invalidation, toolchain-probe refresh,
symlink/source-integrity reconstruction, captured-source cache aliases,
reuse across captured-source edits, and rebuild after an authority/worker
restart. The two worker cases used the real user-systemd executor with a
private HTTP/SQLite authority fixture. Pytest's temporary root was placed under
`/home/ubuntu/.cache` so systemd's `PrivateTmp` would not hide fixture paths.

The restart test's expected reason was corrected from `local_state_untrusted`
to the current closed-vocabulary reason `authority_replica_unconfirmed`; the
result still reports a successful product run with a rebuilt environment and
fresh artifact, and its stale-receipt refusal check passes. An earlier run
using pytest's default `/tmp` root failed during systemd namespace setup because
the fixture path was hidden; it was a test-path issue, not a product failure.

This closes these local fixture checks only. Admitted Flutter/Dart and native
compiler invalidation, cross-worker reuse, authority/worker rollout, and live
cleanup/rollback readback remain open. No shared worker, authority, config,
default-selection, full-E2E, or publishing operation was performed.

## Current-main focused rerun — 2026-10-10 10:12 UTC

Fast-forwarded this task worktree to Livestack `origin/main`
`92cdbf5ecb9c1e74a7476a4f4f2dfefd48960e4c` and reran the same six targeted
cases on `lappy-bellinzona` with Python 3.13.15 and pytest 9.1.1. All six
passed, including the corrected restart-receipt reason expectation. The run
used a home-directory pytest base directory so systemd `PrivateTmp` could see
fixture paths. This is local fixture evidence only; admitted Dart/native and
ABI invalidation, cross-worker reuse, and live rollout/rollback remain open.

The stale restart-test reason expectation was corrected and pushed separately
to Livestack `main` as `45a44cca` after the six-test rerun passed. The commit
changes only the expected receipt reason from
`local_state_untrusted` to `authority_replica_unconfirmed`; it does not close
the remaining admitted-invalidation or rollout requirements.

`openspec validate reuse-task-environments` passes in this task worktree after
the ledger update.

## Receipt and cleanup fixture recheck — 2026-10-10 10:15 UTC

Six targeted tests passed on `lappy-bellinzona` against Livestack commit
`45a44cca`:
running-job cancellation kills the child before capacity is advertised again,
scope closure cancels queued and running work while holding its worker through
cleanup, parked storage eviction reports deletion, profile mismatch rebuilds
and relocates a replica, completion parks a generation after cleanup, and a
returning worker removes only a stale generation. Pytest used a home-directory
base path for systemd `PrivateTmp`. These are local fixture results; admitted
installed-handler receipt and canceled-descendant acceptance remain open.

## Task-scope cancellation integration — 2026-10-10 10:18 UTC

Added and ran `test_scope_close_cancels_task_environment_descendants_after_cleanup`
against the worker fixture: 1 passed. The test is now on LiveStack `main` as
`ddaa830e` (based on `45a44cca`). It closes a
scope with one environment-backed child process running and a second job queued,
then verifies both jobs are cancelled, the child cgroup is empty, the worker has
no cleanup attempt left, and the interrupted environment is marked for rebuild.
This proves fixture-level cascade cleanup; the admitted installed-handler
receipt remains a separate open acceptance item.
