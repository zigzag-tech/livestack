# Independent acceptance triage — 2026-10-11

Read-only acceptance handoff from the landed Livestack upload fix and paired
Benchday change. No source/test file or live worker was changed for this
triage. Full operator details are in
`/tmp/taskenv-independent-acceptance-handoff-20261011.md` on lappy-bellinzona.

## Recorded test results

The home-based-temp run of `node-py/tests/test_workload_worker.py` reported
75 passed, 6 skipped, and 2 failed in 150.18 seconds. The failures are:

1. `test_transient_failure_during_result_upload_is_retried` expects the old
   log text `result upload: authority unreachable`. The job succeeds after
   its two simulated resets; current
   `node-py/livestack_node/workloads/route_kinds.py` logs
   `upload <digest> chunk at 0 failed (...); retrying` at line 138. Proposed
   patch: assert the two current retry warnings instead of the obsolete text,
   retaining the success and consumed-reset checks. The journal-empty check
   exists after the failing assertion but was not reached in this run. No
   product behavior change is indicated.
2. `test_receipt_resources_move_by_the_known_amount_and_say_where_they_came_from`
   expects at least 21.6 MiB of `disk_delta_bytes` after a 24 MiB write, but
   reads zero. Its workspace is under the pytest `tmp_path` on `/home`, which
   is Btrfs. A separate 24 MiB write/flush/fsync positive control on the same
   mount also produced zero from the current `statvfs` measurement in
   `node-py/livestack_node/workloads/resource_usage.py`. Keep the receipt
   assertion; rerun this integration control on the intended bounded ext4
   worker filesystem after its separate quota volume/helper exists. No
   product-code change is justified by the Btrfs fixture alone.

The Benchday bundle suite reported 6 passed, 1 failed, and 3 skipped in
4.15 seconds. The Apple bundle test fails when isolated Python imports
`harmony_flutter_check.py`: it imports `flutter_device_target.py`, which the
Apple bundle source list omits even though the helper exists in `scripts/`.
Proposed patch: add that helper to the Apple bundle’s copied Python closure,
assert it is staged, and retain the isolated-import check.

## Paths and admission

- Livestack test: `node-py/tests/test_workload_worker.py` (current owner lookup
  returns no claim; left unchanged).
- Benchday builder/test: `scripts/build-handler-bundle.mjs` and
  `scripts/build-handler-bundle.test.mjs` (both were among the refused writer
  paths; left unchanged). The importing handler is
  `scripts/harmony_flutter_check.py`; the existing dependency is
  `scripts/flutter_device_target.py`.
- The ARM implementation’s refused files remain
  `scripts/rust-remote.sh`, `scripts/submit_rust_check.py`,
  `scripts/harmony_rust_check.py`, `scripts/build-handler-bundle.mjs`,
  `scripts/test_submit_rust_check.py`,
  `scripts/test_task_environment_handler_release.py`, and
  `scripts/build-handler-bundle.test.mjs`.
- Read-only SQLite count for `pane_id='herdr:wD:pE'` in
  `~/.local/state/benchday-daemon/streams/intents.sqlite` is still zero.
  The owner handles the genuine launch/ancestry repair. No binding or identity
  was fabricated and no new writer claim was attempted.

Current writer readback (2026-10-11 08:06 UTC): `benchday agent whoami` returns
`unresolved: no_agent_pane`; the pane-binding count remains zero. In the paired
Benchday source, `daemon/src/streams/attestation.rs` still discovers only live
tmux roots, while transcript indexing separately extends herdr roots. This
confirms the managed shell still lacks a launch-observed binding. The owner
handles that repair; no writer claim was retried.

## Additive package and rollback evidence

- Local candidate `/tmp/livestack-taskenv-worker-000e253f-review` is from
  landed commit `000e253fcce1c1e2a6f4c252e944d77675496bd9`; its `RELEASE.json`
  records 258 files and content SHA-256
  `4a70d622230ab975395646a75814435dd30fd46af1a0130e1045839d6dabb975`.
  `build-worker-release.py verify <candidate> <candidate>` returned
  `IDENTICAL`. This is local self-verification only. It is a generic worker
  package, not the ARM Rust handler, and is not staged on the Mac guest.
- `/tmp/livestack-workload-worker-rust.candidate.service` has SHA-256
  `f0ede9c47d6f3be3335154014b29e39aa699d6a755503426d77ca86cfb768e3c`;
  `systemd-analyze verify` passed syntax only. No separate config was created
  because there is no authoritative whole-worker config schema/validator and
  the config requires an operator-held token.
- `evidence/rollback-script-disposable-20261011.md` proves only the authority
  deployment script’s disposable failure branch, with a stubbed candidate
  checker. It is not production worker rollback evidence. The worker rollback
  procedure is in `evidence/worker-rollout-handoff-20261011-0254utc.md`; a live
  worker rollback remains untested.
- The authority snapshot is `c2930611`; the controller is observe-only,
  automatic selection remains disabled, and no aarch64 Rust handler or ARM
  installed-handler digest is reported. The ARM guest root is ext4 without
  `prjquota`; storage provisioning and an authoritative config validator remain
  gates. The x86 and ARM Rust/Cargo versions are 1.98.1 and 1.99.0, so the
  same-handle ABI control is not yet eligible.

The owner’s later release correction is recorded in
`evidence/release-status-correction-20261011.md`: daemon 3450 succeeded on all
five targets and completed publication under train `20261011T033118Z-90012`.
It is no longer a build or worker-drain hold. Fresh task-E2E attempt/cleanup
readback remains a separate prerequisite for any target-worker action.

## Focused acceptance rerun — 2026-10-11 19:39 UTC

Current worktree revisions are Benchday
`403c938bd594b856f99834563402acd57952ee05` and Livestack
`7230d7db94dc7e64e7975467ee9862f5e13e0490`; both equal their `origin/main`.
The prior full worker result (75 passed, 6 skipped, 2 failed) was not repeated;
the two failures were rerun directly with Python 3.13.15 / pytest 9.1.1 and a
home-based pytest temp directory. Both reproduce:

1. `test_transient_failure_during_result_upload_is_retried` fails only at its
   stale text assertion, `node-py/tests/test_workload_worker.py:1697`. The
   worker step succeeds, both simulated resets are consumed, and the job state
   is `succeeded`; the current retry warnings come from
   `node-py/livestack_node/workloads/route_kinds.py:138`. Since the stale
   assertion stops execution, the following journal-empty assertion at line
   1698 was not reached and is not proven by this run. Minimal patch: assert
   two warning records containing the retry marker (rather than the old
   literal phrase), then retain the success, reset-count, and journal-empty
   assertions. No worker behavior change is indicated.

   Proposed replacement for line 1697 (not applied):

   ```python
   assert sum(record.levelno == logging.WARNING and "retrying" in record.getMessage()
              for record in caplog.records) == 2
   ```
2. `test_receipt_resources_move_by_the_known_amount_and_say_where_they_came_from`
   reads `disk_delta_bytes == 0` after its known 24 MiB write and fails the
   existing >=21.6 MiB assertion at line 2003. The test temp is on Btrfs
   (`/dev/mapper/root[/@home]`); the measurement uses filesystem-wide
   `statvfs` in `node-py/livestack_node/workloads/resource_usage.py:47-55`.
   Keep the threshold. Re-run this control on the intended bounded ext4 worker
   filesystem only after its separate quota volume/helper and positive control
   exist; this Btrfs result does not establish a product-code defect.

The Benchday bundle test was rerun with
`node --test scripts/build-handler-bundle.test.mjs`: 6 passed, 1 failed,
3 skipped. The Apple case fails under isolated Python import with
`ModuleNotFoundError: No module named 'flutter_device_target'`. The Apple
source list in `scripts/build-handler-bundle.mjs:323-328` omits the helper,
while the non-Apple list at line 329 includes it; the importer is
`scripts/harmony_flutter_check.py:72`. Minimal patch: add
`flutter_device_target.py` to the Apple closure and to the staged-file
assertion in `scripts/build-handler-bundle.test.mjs:416-420`; retain the
existing isolated import behavior check at lines 406-411.

The scoped writer readback remains unresolved: `benchday agent whoami` returns
`no_agent_pane`; the read-only binding count for `herdr:wD:pE` is 0; supported
`benchday owner-of` reads return `owner: null` with source `claims` for the
listed Benchday source/test paths and the Livestack worker test. No writer
claim was attempted and no refused source/test path was edited. The owner owns
the supervised launch/ancestry repair and can take the legitimate scoped
claims. No pane binding or identity was fabricated.

The additive artifacts were rechecked, but they are not an ARM release:

- `/tmp/livestack-taskenv-worker-000e253f-review/node-py/RELEASE.json` records
  landed generic package commit `000e253fcce1c1e2a6f4c252e944d77675496bd9`,
  258 files, and content SHA-256
  `4a70d622230ab975395646a75814435dd30fd46af1a0130e1045839d6dabb975`.
  `node-py/scripts/build-worker-release.py verify <candidate> <candidate>`
  returned `IDENTICAL`; this is a local self-check, not deployed-host readback.
- `/tmp/livestack-workload-worker-rust.candidate.service` has SHA-256
  `f0ede9c47d6f3be3335154014b29e39aa699d6a755503426d77ca86cfb768e3c`;
  `systemd-analyze verify` passed syntax. It points at the generic
  `livestack-000e253f` package and a config path for which no schema-validated
  candidate exists. Neither file is installed.
- The disposable rollback result in
  `evidence/rollback-script-disposable-20261011.md` exercises the authority
  deploy script's failure branch only. The worker rollback/readback procedure
  is in `evidence/worker-rollout-handoff-20261011-0254utc.md`; production
  worker rollback remains untested. Controller remains observe-only; automatic
  selection remains disabled. No worker/service was restarted or changed.

Full review handoff: `/tmp/taskenv-independent-acceptance-handoff-20261011-current.md`.

## Additional local acceptance rerun — 2026-10-11 19:45 UTC

Seven focused Livestack fixture tests passed in 8.38 seconds using Python
3.13.15 / pytest 9.1.1 and a home-based temp directory. They cover incompatible
profile rebuild plus replica relocation, toolchain-probe refresh invalidation,
stale-replica cleanup generation, writer exclusion and parked receipt,
generation-scoped cleanup after worker return, stale-receipt rejection after
worker/authority restart, and running/queued descendant cancellation after
scope close. These are local fixture checks; they do not prove the installed
ARM handler, ext4 quota helper, or live admitted cleanup path.

Four Benchday companion fixtures also passed, one each: source mirror edit,
deletion and lockfile invalidation; authority handoff with parked-source reuse;
relocation reconstruction and expiry; and full-E2E/publishing legacy
submission plus environment-reference refusal. They were run through the
repo's `executeWithPinnedRuntime()` helper, which materializes the pinned
runtime before invoking the named unittest. A direct unittest invocation
without that materialization failed during setup trying to read `schema.sql`
through a zip path; that attempt is invalid test setup, not a product result.
These local fixtures do not run the coalesced full E2E or publishing.

The live/installed-handler acceptance, ARM implementation and canary, worker
rollout, ext4 quota setup, rollback, and automatic-selection gates remain open.

The test-only retry assertion proposal is staged for owner review, not applied,
at `/tmp/livestack-acceptance-fix-20261011.patch`; `git apply --check` passes
against this worktree. The separate Benchday Apple closure/test proposal is
`/tmp/benchday-apple-bundle-fix-20261011.patch`; its `git apply --check` also
passes against the Benchday worktree. No patch is proposed for the disk-delta
threshold pending the bounded-ext4 positive control.

## Fresh paired-worktree recheck — 2026-10-11 19:59 UTC

Livestack remains at `7230d7db94dc7e64e7975467ee9862f5e13e0490`. Benchday
advanced to `912b6d16aa6be1b721e398fbd84dedd2773c31a5`; its worktree was
fast-forwarded after confirming no overlap with dirty task artifacts. Current
source still lacks the ARM caller/handler route, and Apple bundling still
omits `flutter_device_target.py`. Both unapplied patch files continue to pass
`git apply --check` in their respective worktrees.

Identity and ownership remain unresolved: `benchday agent whoami` is
`no_agent_pane`; `herdr:wD:pE` has no binding; and owner lookup reports no
claims for affected paths. No source/test file or worker state was changed.

Post-recheck validation: `openspec validate --specs` passed 12/12;
`git diff --check` and `git apply --check
/tmp/livestack-acceptance-fix-20261011.patch` both passed.

## Owner-landed acceptance fixes and refreshed worktrees

The owner reports Livestack commit
94128fcf17d58d0ed591a7887ee06b4c3f26328b updated the upload-retry test to
use the current route_kinds.transport.dial behavior and handle the legacy
offset probe; that focused test passes. The earlier stale-log assertion
failure is superseded by this landed test change. The owner also reports
Benchday commit e4a5ff54c7ee2d593ceaf4d002e06f8bea44b343 fixed the Apple
bundle closure and its suite passed 7/7.

Both task worktrees were fetched and fast-forwarded without overlap with their
dirty OpenSpec paths. Livestack is at
7b89084242765b9abc4d1a0592750656bdc694f6, equal to origin/main; Benchday is
at 42075db7f335ad5a17aeb87ff59e2c5a2532269f, equal to origin/main. This
session did not rerun those tests. The remaining known worker-suite issue is
the statvfs disk-delta check on Btrfs; retain its threshold and rerun on the
bounded ext4 worker filesystem after quota support and a positive control.
ARM implementation, quota setup, worker rollout, cleanup/readback, and rollback
remain open.

## Goal continuation acceptance rerun — 2026-10-11

Livestack task worktree is at 7b89084242765b9abc4d1a0592750656bdc694f6,
equal to origin/main. It includes owner-landed commit
94128fcf17d58d0ed591a7887ee06b4c3f26328b, which updates the upload-retry test
for the current worker route. The focused retry test passes.

Home-temp local verification:
- node-py/tests/test_task_environments.py: 32 passed in 12.10 s. These are
  local fixtures, not admitted-worker acceptance.
- node-py/tests/test_workload_worker.py: 76 passed, 6 skipped, 1 failed in
  155.88 s. The retry test passes. The only failure is
  test_receipt_resources_move_by_the_known_amount_and_say_where_they_came_from:
  disk_delta_bytes remains zero after a 24 MiB write; pytest temp is on
  Btrfs (/dev/mapper/root[/@home]), where this statvfs-based control does not
  report the expected delta. Keep the assertion threshold. Repeat on the
  intended bounded ext4 worker filesystem after quota support and a positive
  control exist.
- The owner-reported Benchday Apple bundle suite was also rerun locally and
  passed 7/7 with 3 skips.

No shared worker job, service, or configuration was changed. The supported
Benchday identity readback remains no_agent_pane; the supervised launch and
herdr ancestry repair is owner-owned. Livestack source/cache and compiler/ABI
acceptance, ARM worker enrollment, quota, rollout, cleanup/readback, and
production rollback remain open.
