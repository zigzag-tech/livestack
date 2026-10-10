# Tasks

Current live recheck (2026-10-10 10:35 UTC): the authority now runs
`livestack-486cf6e5`, including the source-upload capacity preflight. The
Benchday source bundle was accepted as job `44e6910403f14abeb8bf7042a4dcb96e`,
but it remains queued with no attempt, so source reconciliation and compiler
reuse are not yet verified. Workers 1 and 2 are busy, workers 3 and 4 lack the
environment profile, and worker 5 is draining. Keep source-mirror acceptance
and worker/SDK rollout open. Details: `evidence/live-recheck-20261010-1035utc.md`.

Follow-up (2026-10-10 10:45 UTC): admitted job
`44e6910403f14abeb8bf7042a4dcb96e` succeeded with both Cargo caches reused and
the environment parked after cleanup. Its measured queue was 567.760 s and
compile phase 2.102 s. Current-main job `7efa3d57f8e24fc0bbbcc15dc08754b1` is
accepted but still queued, without compute assigned. Source invalidation and
broader worker rollout remain open; see
`evidence/live-recheck-20261010-1045utc.md`.

Follow-up (2026-10-10 10:56 UTC): the queued `7efa3d57f8e24fc0bbbcc15dc08754b1`
request used an older source after main changed Rust inputs. It had no attempts
and was withdrawn before starting. Current main
`3062f53e79d89fc4e56b5b6d72bf09b8febd030b` was submitted as job
`5d83ba77da7749649452db71100b8cac` on the same saved environment; it remains
queued with no attempt. The handle remains parked with no compute assigned.
Details: `evidence/live-recheck-20261010-1045utc.md`.

Follow-up (2026-10-10 10:58 UTC): current-main job
`5d83ba77da7749649452db71100b8cac` succeeded on `zz-joe-e2e-2` using the same
environment. Both Cargo caches were reused and the environment parked at
generation 20 after cleanup. Queue was 36.500 s; compilation was 6.078 s.
Native/symlink/lockfile/toolchain invalidation cases and worker/SDK rollout
remain open. See `evidence/live-recheck-20261010-1045utc.md`.

Follow-up (2026-10-10 11:02 UTC): explicit same-handle repeat
`c4ff2a01901749548f634175ec903032` passed on the same worker at generation 21.
Both Cargo caches were reused. Only OpenSpec evidence changed between source
captures; Rust inputs did not. Queue was 0.533 s and compile was 0.281 s,
compared with 6.078 s for the preceding changed-source compile. This is a warm
repeat, not a cold-workspace comparison, and each invocation still had a
separate request and queue phase. The environment parked after cleanup. See
`evidence/live-recheck-20261010-1045utc.md`.

Follow-up (2026-10-10 12:21 UTC): the current-source Rust `check rust` job
`40a26d21917e4baab2389c80ca276bed` succeeded on `zz-joe-e2e-1` using the
retained handle last used on `zz-joe-e2e-2`. Both Cargo cache components were
reused and source changes were applied incrementally; the environment parked
at generation 22 after cleanup. This verifies a current daemon-source update
on the real compiler handler, but does not close the remaining Flutter
Dart/native, lockfile, symlink, toolchain or ABI invalidation acceptance. See
`evidence/live-recheck-20261010-1221utc.md`.

The same recheck built worker release `4f282ce0` from current Livestack main
(258 files, content hash
`8917b440ac5c11055a46f396ed75d290df5e12de2c508faa5bb5ab83534cbba3`). The
active `zz-joe-e2e-1` and `-2` units still point at `livestack-377bb4e4`
(content hash `7cf90d52f10d4e68e9442aa493c8f8f67d0ec2a6c66526893017f9dd0d05d2c2`,
246 files); verification found 12 files only in the current candidate and 6
changed files, with no hand-edited deployed copies. The authority unit still
points at `livestack-486cf6e5`. Candidate build and remote reads made no live
changes; authority/worker rollout and readback remain open. Details:
`evidence/live-recheck-20261010-1221utc.md`.

## 1. Durable request and agent interfaces

- [x] 1.1 Add schema 3, strict environment key/handle validation and authenticated capabilities; verify real HTTP legacy identity, version refusal and unsupported-before-upload checks. Schema-3/HTTP/legacy/refusal controls passed in the focused authority/worker/CLI/scheduler suite: 91 passed, 6 skipped. Ledger: bounded capability/refusal outcomes with no secrets.
- [x] 1.2 Add atomic owner-scoped environment resolution and job binding; verify real durable database/HTTP restart, lost-response replay, changed-request conflict, foreign handle and same-key/different-owner cases. `test_environment_key_resolution_is_atomic_durable_and_owner_scoped` reopens the database and HTTP authority, replays the accepted request through the restarted server, and checks changed input, foreign handle and independent owner; focused suite: 91 passed, 6 skipped. Ledger: creation/resolution joined to accepted job.
- [x] 1.3 Add environment inspection and bounded SDK responses; verify scoped not-found, retained/expired handle, unknown timing and response byte/deadline refusal. Owner/expiry and no-new-job inspection controls plus the completion receipt and delayed-authority/oversized-view checks passed; focused suite: 91 passed, 6 skipped. Ledger: reads return no new history; timeout and byte-bound failures reach the caller.
- [x] 1.4 Extend the existing workload CLI submit/get surface and Python SDK with selectors, capability negotiation and environment inspection; execute documented commands against a disposable authority and verify JSON/stderr/exit behavior and conflicting selectors. Disposable CLI coverage exercises key and handle selectors, opt-out, job and environment inspection, JSON, conflicts and accepted job IDs; focused suite: 91 passed, 6 skipped. Ledger: preserve accepted IDs and no hidden resubmission.

## 2. Placement, ownership and admission

- [x] 2.1 Add bounded replica reports and batched registry lookup to the scheduler snapshot; verify increasing environment counts do not increase database round trips and unknown compatibility never becomes a hit. Worker report 64-entry bound, unknown-compatibility cold placement and trace-callback comparison at 1 versus 24 environments pass in the focused suite: 91 passed, 6 skipped. Ledger: bounded candidate values and exclusions.
- [x] 2.2 Atomically combine environment writer generation and ordinary host resource admission; verify concurrent same-handle requests, independent environments and same-host multi-worker reuse with a real authority. Same-handle exclusion, independent-handle concurrent admission and same-host handoff pass in the focused suite: 91 passed, 6 skipped. Ledger: environment-busy vs resource wait vs admitted.
- [x] 2.3 Add compatible-host preference and durable capped affinity; verify busy preferred host, available alternative, unknown ETA, restarted authority and policy-excluded old host cases. The 15-second affinity wait survives authority restart, reports no fabricated ETA, and yields to cold placement after expiry or preferred-host drain; focused suite: 91 passed, 6 skipped. Ledger: measured/unknown estimate components, affinity start/expiry and actual choice.
- [x] 2.4 Enforce installed development/task-E2E purpose and bounded exact-ID request shape; verify the installed task handler rejects empty, unknown and full-suite selection against captured source before preparation, while full/coalesced E2E, publishing/release and caller-spoofed purpose refuse and legacy no-environment orchestration still works. Livestack exact-ID/forbidden-purpose controls and Benchday's captured-source task-E2E adapter controls passed; focused Livestack suite: 91 passed, 6 skipped. Ledger: environment_scope_forbidden and exact task selection identity.
- [x] 2.5 Scope same-host replica reconciliation to declared worker profiles; verify partial and empty reports preserve omitted-profile rows, stale rows within declared profiles are removed, and out-of-scope replicas are refused. The focused report tests passed 2/2, and `test_workload_environments.py` plus `test_task_environments.py` passed 52/52 on Python 3.13.15.

## 3. Bounded worker environments

Validation update (2026-10-03): On zz-joe at Livestack source `c881e2d1`, `test_worker_reuses_task_environment_across_captured_source_edits`, `test_cancel_running_job_reconciles_before_readvertising_capacity`, and `test_worker_and_authority_restart_rebuild_unconfirmed_task_environment` passed 3/3 through a private HTTP/SQLite authority and real systemd worker. The pytest/uv cache and test workspace were temporary and removed. These use the test's synthetic handler, so admitted compiler/ABI validation, rootless container/socket cleanup, live-worker readback, and returning-worker cleanup across hosts remain open. After rebasing the eviction-expectation fix onto current Livestack `b432e8bf4faade380fcfeeba375bccf74c5e86ee`, the three-file worker/store/lease suite passed 113 tests with 6 skipped in 93.55 s on `lappy-bellinzona` (Python 3.13.15, pytest 9.1.1). This is source-regression evidence only; admitted compiler/ABI, quota enrollment, container/socket cleanup, and live readback remain open. After adding the disposable rollback integration in Livestack commit `886f0067`, `test_task_environments.py` and `test_workload_environments.py` passed 38/38 in 8.19 s on `lappy-bellinzona` (Python 3.13.15, pytest 9.1.1); these fixture tests do not close persistent kernel quota or host drain/deprovision acceptance.

- [x] 3.1 Provision environment storage with per-replica and aggregate kernel bounds on the first supported Linux backend; verify an actual child overflow, owner isolation and physical-host disk accounting. Ledger: disk admission/refusal and retained byte readings. On zz-joe (2026-10-06), a persistent 128 GiB ext4 loop volume with `prjquota` is mounted at `/var/lib/livestack-workloads/environment-hosts/zz-joe/task-environments`; ext4 has no hidden 5% reserve, while the store keeps its 1 GiB software reserve. `fallocate` changed host `/` availability by 136,357,412,864 bytes (volume plus metadata). The quota helper's 4 MiB positive control produced `EDQUOT` at 4,190,208 bytes for one project while an independent project wrote 3,145,728 bytes; both usages returned to zero after cleanup. The actual `TaskEnvironmentStore` clamped the aggregate cap to the mounted filesystem's available capacity (133,071,638,528 bytes), reported all three profiles, persisted and read back a parked 49,152-byte usage receipt, admitted four replicas totaling the cap, refused a fifth, and returned all project usage to zero after cleanup. This closes bounded storage and aggregate accounting; installation into live worker services and their readback remain task 4.4.
- [ ] 3.2 Implement complete source mirror reconciliation and handler-owned compatibility/cache components; verify real unchanged/Dart-edit/native-edit/deleted-file/symlink/lockfile/compiler/ABI sequences without stale artifact use. Ledger: source/recipe identities and each reuse/invalidation outcome. Private fixtures at Livestack `f86a8f78`: `test_task_environments.py` passed 12/12, `test_workload_environments.py` passed 19/19, and `test_worker_reuses_task_environment_across_captured_source_edits` passed 1/1. An extension of that worker test passed 1/1 on zz-joe against a real systemd worker and disposable HTTP/SQLite authority: repeated schema-3 jobs reused the same handle/build cache, the second handler observed a file deleted from its captured source and the new 0755 mode. This uses a synthetic handler; admitted Dart/native/compiler/ABI and real toolchain invalidation sequences remain open. Stable-path follow-up: The admitted stable-path Rust pair now verifies the unchanged `check cli` case: the same handle/cache identities were reused and the repeat had no Cargo `Compiling` or `Checking` output. Dart/native edits, deleted-file/symlink/lockfile controls, and compatibility invalidations remain open; see evidence/cargo-cache-stable-path-repeat-20261009.md. Follow-up 2026-10-09: the admitted changed-source check daemon job reports source_updated_incrementally and reused both Cargo cache components on the same handle; the stable unchanged-source repeat produced zero Cargo Compiling/Checking lines. Dart edits, deleted-file/symlink, lockfile and toolchain invalidation controls remain open. Recheck 2026-10-10: 55 local task-environment/authority tests pass, including lockfile/toolchain compatibility cases. The first systemd-backed worker attempt used a pytest temp path hidden by `PrivateTmp`; a corrected six-case rerun on `lappy-bellinzona` at 10:12 UTC passed using a home-directory `--basetemp`, including source-edit and authority/worker-restart cases. Real admitted Dart/delete sequences are recorded in `evidence/alternating-benchmark-20261010.md`; native, symlink, lockfile and toolchain/ABI worker sequences remain open. Admitted lockfile follow-up (2026-10-10): Rust job `28c2f2f039e1481bbd63ba265b74e16c` succeeded after a lockfile-input change and explicitly invalidated both `cargo-home` and `cargo-target`; see `evidence/rust-lockfile-invalidation-20261010.md`. Symlink and toolchain/ABI worker controls remain open. Local follow-up 2026-10-10: `test_profile_probe_refresh_invalidates_cache_after_toolchain_replacement` passes as a fixture check; real admitted toolchain/ABI identity changes remain unverified. See `evidence/live-purpose-boundary-20261010.md`. Latest admitted source-freshness follow-up (2026-10-10): Benchday jobs c422bd55d3c54cc28e2f2ecf17c2dd55 and 0b74507551e640f989c50fdb815cd940 compiled an internal symlink target, then produced the expected E0583 after link removal; the changed Rust source/repeat/restore sequence also passed. Both reused the same parked environment per sequence and released resources after each attempt. See evidence/alternating-benchmark-20261010.md. The native-edit and symlink sub-controls are closed; real compiler/ABI identity invalidation remains open.

  Captured source alias/cache regression (2026-10-08): added a focused worker-store case where the cache profile names `source/packages/core/node_modules` and `packages/core/package-lock.json`, while the captured source maps `packages/core` to private `packages/core-source`. Before the fix, cache directory creation materialized the alias as a real directory, so Benchday's private-link restoration correctly refused the source. Cache roots and cache-input identities now resolve through the validated alias manifest, while receipts retain the profile's declared paths and private-link checks remain unchanged. The new case and both related worker-store modules pass 44/44 on the Livestack task worktree; broader admitted compiler/ABI acceptance remains open.

  Source/cache bound correction (2026-10-09): the admitted task-E2E job `85949aa9d0544490b83aa9490121e0b6` for `fleet-workload.task-environment-source-and-cache-freshness` reached a partial assertion pass on the captured source, but both worker attempts terminated with infrastructure error `environment source tree exceeded its file bound` and emitted no bounded task result artifact. It is not task-E2E acceptance or reuse evidence. The worker now reconciles immutable source entries separately from declared cache descendants, caps each cache component at 100,000 entries, and verifies cache symlinks under that separate bound. The new reuse/source refusal regression and all of `test_task_environments.py` passed 29/29 locally; admitted compiler/toolchain acceptance remains open.

  Cargo path diagnosis (2026-10-09): admitted job `bbbe018da9c2400c9a1774d39028feeb` reported both Cargo components reused, but its log still compiled a broad dependency graph after a non-Rust source update. The worker had bound the retained source under an attempt-ID-specific absolute path, which also changed the target path Cargo fingerprints. The worker now uses a provisioned, host-stable execution-view path inside each attempt's isolated mount namespace. This is an implementation hypothesis pending an admitted compiler repeat; no time saving is claimed. See `evidence/cargo-cache-path-instability-20261009.md`.
- [x] 3.3 Separate retained disk from attempt runtime and implement park only after supervised cleanup; verify real descendant, container/socket, cancellation and lease-expiry controls leave no compute claim when parked. Ledger: cleanup pending/confirmed and resources released. The cancellation integration `test_cancel_running_job_reconciles_before_readvertising_capacity` passed on zz-joe: its task-environment handler spawned a child inside the systemd cgroup; after cancellation the cgroup was empty or gone, environment state was `rebuild_required`, no local replica was advertised, and no cleanup attempt remained. The current candidate rootless integration `test_lease_expiry_cleans_rootless_container_before_releasing_task_environment` passed 1/1 in 13.96 s on zz-joe against a private HTTP/SQLite authority; it verified the system-manager unit/cgroup, container PID and socket teardown, retained build-cache disk, no replica advertisement and zero running/cleanup claims. The private worker success controls also confirm parking after attempt cleanup. This closes implementation-level cancellation, lease-expiry and retained-disk cleanup; installed-worker capability/cleanup readback remains task 4.4.
- [x] 3.4 Reconcile worker/authority restart and stale generation writes, using new private reconstruction when the old replica is uncertain; verify old-result fencing and returning-worker cleanup with real processes. Ledger: fence/generation/rebuild reason. On zz-joe at source `c5446860` plus the cleanup changes, the focused cleanup/reuse controls passed 3/3 (`test_stale_replica_cleanup_yields_to_writer_and_preserves_a_newer_marker`, `test_returning_worker_gets_generation_scoped_cleanup_for_stale_replicas`, and `test_worker_reuses_task_environment_across_captured_source_edits`; 87 deselected). The latter used two systemd-backed worker identities with separate environment roots and a private HTTP/SQLite authority: the replacement worker committed generation 3, then the returning worker received exact generation-2 cleanup; its stale directory disappeared while the replacement replica/registry remained. These host identities share zz-joe, so independent-machine connectivity was not exercised. Prior `test_worker_and_authority_restart_rebuild_unconfirmed_task_environment` evidence at `dd9cae9` verifies partial-cache reconstruction after both restarts and rejects a late old-boot completion with HTTP 409.
- [x] 3.5 Enforce metadata/replica/byte/age bounds and bounded batched sweeps; verify active protection, idle/absolute expiry, unset-window refusal, deletion failure charges, eviction/recreation and host drain/deprovision independence. Ledger: each eviction/refusal/degraded sweep outcome. Local fixture controls pass for active-writer protection, idle eviction/recreation, absolute expiry despite recent reuse, deletion-failure quota charging/retry, and a lowered 32-entry sweep over 64 environments with a second pass; attempts to raise the cap are refused (`test_task_environments.py`: 5 targeted controls). Authority-level disabled-retention capability/admission refusal also passes (`test_workload_environments.py -k expiry_recreates`: 1 passed). The added `test_parked_environment_does_not_block_host_deprovision` passes 1/1 locally: a parked `TaskEnvironmentStore` replica survives the real `HostBroker.leases_on`, `_drain_blocked`, and `fleet_ops_api.deprovision` path, including provider teardown. The listed implementation controls complete this item; live admitted-worker policy readback remains under task 4.4.
- [x] 3.6 Preserve a parked prior generation while a newer writer on the same physical host evaluates reuse, while retaining stale cleanup on relocation. `test_registration_preserves_parked_generation_for_same_host_writer` passes both same-host preservation and different-host cleanup cases (2/2); against the prior authority code, the same-host case returned stale cleanup and failed. A worker-level systemd integration could not run on this host because its per-user systemd manager is degraded; the authority regression directly verifies cleanup instructions and registry retention. Ledger: no local bytes are deleted before same-host reuse is evaluated; relocation still returns exact cleanup identity.

## 4. Receipts, consumer acceptance and rollout

- [x] 4.1 Produce bounded per-attempt environment receipts and measured phase timing; verify known compilation positive controls, unavailable measurements, precise reuse rejection reasons and recording failure propagation. Ledger: join all environment/placement/outcome records by decision/job/attempt/handle/generation. Unit and real systemd controls verify known and unknown timing, six distinct replica refusal reasons, timing-write failure propagation, and bounded receipts. Admitted stable-path Rust checks and the changed-source daemon check provide successful compiler receipts and real unavailable test timing. The installed completion ledger row links to admission through parent decision ID and records the same job, attempt, worker, host, handle and generation 11. See evidence/cargo-cache-stable-path-repeat-20261009.md.

  Benchday's focused `RustSubmissionTest.test_phase_timing_write_failure_propagates_before_compilation` passed 1/1 against Livestack worktree `8bd2c43`: a forced timing-file write failure aborts before compiler subprocess launch and leaves no receipt. This local negative control does not satisfy the admitted compilation positive control or ledger-join requirement.
- [x] 4.2 Add real integration/state-machine coverage for exclusive authorized writer, no parked compute and stale-generation exclusion; run the glob-discovered relevant checks on an admitted build/test host. Ledger: retain test evidence/failed controls, no production probes presented as tests. On zz-joe, the glob-discovered private-authority/environment suite and selected systemd worker lifecycle controls passed 34/34 (`tests/test_*environment*.py tests/test_workload_worker*.py -k 'environment or cancel_running_job or authority_outage_longer_than_the_lease'`, Livestack source `f86a8f78`). This covered exclusive writers, affinity waits without attempts/compute claims, stale-generation fencing, cancellation/lease cleanup, and a real systemd worker reuse attempt; it did not use the live Harmony authority or claim rollout completion.
- [x] 4.3 Update `HARMONY.md`, workload CLI/API examples and `_plans/durable-workloads.md` with the implemented task workflow and full-E2E/publishing exclusion; execute the documented CLI sequence against disposable services. Disposable CLI coverage executes stable-key and handle submission, explicit opt-out, job/environment inspection and existing response IDs; focused suite: 91 passed, 6 skipped. Ledger: no new durable state; examples preserve existing observation/result IDs.
- [ ] 4.4 Roll authority, supported development/task-E2E workers and consumer SDK via normal release/drain procedures; verify actual capability, quota, cleanup and forbidden-purpose readback before instruction/default activation. The authority's current authenticated capability endpoint reports environment policy version 1: Flutter, Rust, and task-E2E are allowed; full-E2E, dependency, commerce, and release/publishing handlers are forbidden. Rust job `36de28bc5bc14f02a6cf9bcfc7deb716` is accepted against the parked handle but still queued without an attempt. Workers 1, 3, and 4 lack the Rust environment profile, worker 2 is busy, and worker 5 is claim-disabled. This corrects the earlier observation that environment policy was disabled, but does not identify the authority's immutable release digest or prove cleanup/rollback. The release provenance, broad worker/SDK rollout, admitted compiler acceptance, and rollback readback remain open. See `evidence/live-capability-readback-20261009.md`. Follow-up at 16:42 UTC supersedes the earlier queued readback for job 36de28bc5bc14f02a6cf9bcfc7deb716: it succeeded and returned its handle parked at generation 7. Current-source job bbbe018da9c2400c9a1774d39028feeb remains queued without attempts; workers 1/3/4 lack the Rust profile, worker 2 is busy, and worker 5 is draining. Broad worker/SDK rollout, cleanup/rollback proof and installed decision-ledger readback remain open. See evidence/admitted-rust-resume-20261009.md. Follow-up at 16:50 UTC: live rollout status remains mode observe, with no changes applied. The zz-joe-e2e set requires two claimers but only zz-joe-e2e-2 reports benchday.e2e.task.v1; canary_not_representative blocks rollout acceptance. Keep defaults disabled. The configured caller received 403 from the admin-only status endpoint, so the live decision-ledger status is not certified. See evidence/live-rollout-observation-20261009.md. Stable-path follow-up: The stable-path worker release 377bb4e4 is now active on zz-joe-e2e-2 and passed two admitted Rust checks with the environment parked at generation 10. The other eligible worker profiles, authority/SDK rollout provenance, rollback readback, and representative task-E2E canary remain open; see evidence/cargo-cache-stable-path-repeat-20261009.md. Current readback 2026-10-09: the authority service is active on release d55c4c13, and workers 1 and 2 advertise all three task profiles; worker 1 was drained, updated and re-enabled. Observe mode remains in place and automatic selection is disabled. Consumer-default activation, authority/worker rollout provenance and rollback proof remain open. See evidence/rollout-recheck-20261009.md. Fresh 2026-10-10 04:17 UTC readback: policy v1 still allows Flutter/Rust/task-E2E and forbids full E2E/release; Rust and Flutter handles are parked; authority remains on d55c4c13 with 486cf6e5 staged. Three unrelated ZZOPS trains are active, so the abort-mode drain was not retried. See evidence/live-recheck-20261010-0417utc.md. Follow-up 2026-10-10 04:21 UTC: the streams assertion reached terminal failure outside this change scope; two unrelated trains remain active (client-performance admission and full-suite completion). No cancellation, fence hold, or deploy was attempted. See `evidence/live-recheck-20261010-0417utc.md`. Candidate freshness update: Livestack main advanced to `24c34f8d` affecting workload HTTP/result manifests; staged `livestack-486cf6e5` is now stale and must be refreshed/rechecked before deployment. Worker-release follow-up: built current worker candidate from origin/main `e498c8284f77848f1a0c97c6228e585b5266d6e0` (258 files, content hash `8917b440ac5c11055a46f396ed75d290df5e12de2c508faa5bb5ab83534cbba3`). Read-only comparison against zz-joe release `livestack-377bb4e4` found 12 candidate-only files and 6 changed files; every deployed version matches current origin history, no hand edits. This did not deploy it or refresh the separate authority candidate. See `evidence/worker-release-candidate-20261010.md`. Live recheck 2026-10-10 19:21 UTC directly confirmed the authority refuses environment-bound full-E2E and release requests with 403 and creates no job. Rollout remains observe-only; worker 1 is behind, workers 2–5 are unknown, and task-E2E runs are queued behind active work. Provenance, representative canary, rollback readback, and default activation remain open. See `evidence/live-purpose-boundary-20261010.md`. The same local run passed scope-close cancellation of running and queued descendants; this fixture does not prove cleanup on the installed handler. Candidate refresh 2026-10-10 19:31 UTC: release candidate from `origin/main` b7f3151e passed static, boot, worker-registration, and job-round-trip checks using the currently deployed dependency set. It remains a local candidate; the active authority and task workers were not changed. Idle-window rollout, live post-rollout readback, canary, and rollback proof remain open. See `evidence/authority-candidate-precheck-20261010.md`. Latest task-specific canary attempt (2026-10-10): Benchday job 8151336bd90b41cc9c9126c93981a7b4 selected one check and failed before source materialization on both task-E2E workers because the worker-host storage budget was exhausted; its environment is generation 15, rebuild_required, with zero bytes. This is no canary result and no rollout readback. See evidence/live-purpose-boundary-20261010.md.

  Live refresh (2026-10-08): the authority now runs `livestack-c76585e7` with the three development/task-E2E handlers eligible and full-E2E, dependency and publishing handlers forbidden. Worker 2's installed config reports all three profiles and quota bounds, and its handler roster includes Flutter, Rust and task-E2E. The first actual Flutter environment job failed before source materialization (`record byte limit exceeded`, 0 bytes) because the source manifest exceeded the generic 64 KiB encoder bound although the manifest itself permits 16 MiB. Fix `cbeafb06` hashes the raw bounded manifest bytes; the >64 KiB regression and full `test_task_environments.py` passed (20/20). Its immutable worker release was built and verified identical after staging on zz-joe. It is not active: worker 2 is executing shared ZZOPS completion job `fe333995f6ee4ef4a798b8b453142b89` and currently remains claim-enabled; it has not been restarted for this release. Live successful compiler/task-E2E runs, cleanup receipts, restart-safe rollout, and multi-worker readback remain open.

  Rollout-prep refresh (2026-10-06): Benchday bundles were rebuilt from landed commit `43a69d9c6fc94317aef7e7cffc39b494907f67b8` at locked ZZOPS pin `e2e899656a402b1c0a3af386e5b380330d9785a4`; the extracted trimmed payload SHA-256 is `8d76af0adab9878a24097515a8c1f86f20e5efbba4ecbe2407adcba92da63b34` and contains only Flutter check, Rust check and task-E2E packages. Candidate-v5 worker configs preserve both live base hashes and remain mode 0600. Authority candidates were revisioned to `benchday-task-environments-43a69d9c6f`; `load_config()` accepts both, and the final candidate classifies exactly three handlers as eligible and full-E2E/dependency/release handlers as forbidden. No live config or service changed. ZZOPS still reports three queued cargos and two remote trains outstanding. The full-suite process remains active on `zz-joe-e2e-1`; worker 2 has no active record. The generic Livestack client cannot see the ZZOPS-owned job/result handoff, so rollout remains gated pending terminal ZZOPS status and authority-side cleanup confirmation.

  Follow-up queue recheck (2026-10-06, ZZOPS human config): status now shows two dispatched trains. Job `4dfd835369e1454ab93147cea8ba2251` is running full E2E on `zz-joe-e2e-2`; the same worker is reported holding an attempt or cleanup. Worker records also show Flutter compile job `20ee242bfc6146729a8759cfbfff7a15` running on `zz-joe-e2e-1`. The full run `699db5fb1f0d4ea0b2cd1f0574e34057` remains queued behind drain/avoidance reasons. The ZZOPS deploy fence is clear, but both workers remain occupied; the scoped Livestack client still cannot confirm ZZOPS result handoff. No worker or authority was changed.

  Second queue recheck (2026-10-06): ZZOPS human-config status still shows two dispatched full-E2E trains and two queued cargos, with the deploy fence clear. Both Joe workers now have `benchday.e2e.full.v1` attempts in phase `running`: job `699db5fb1f0d4ea0b2cd1f0574e34057`, attempt `da45bf88df844869ad7cd9e0ec11c514`, on `zz-joe-e2e-2`; and job `5f8027bde4f64a4d98af70d5c2ba0e4c`, attempt `a6bab3405a4540399fc6727957fbba04`, on `zz-joe-e2e-1`. These are coalesced ZZOPS runs, not started for this change. Both worker records remain running; rollout and task acceptance stay deferred until ZZOPS terminal status and authority cleanup/result-handoff confirmation.

  Current prep and queue read (2026-10-06): after `origin/main` advanced, the task worktree was fast-forwarded to `e306748753b79055ea6fb43843089fe133705915` (the upstream diff did not touch this task ledger). Bundles were rebuilt at the locked ZZOPS pin; the trimmed archive SHA-256 is `135a4805b07024d7dcc0e8dd1920802379f905f1697b2f4e92552cb6ec57fd93` and contains only Flutter (`1450b6add407e102e397b8f0e92fe4be165228f68bb44ff9b3f0d9b25bd0b5b5`), Rust (`ad0dc9d0ef425d4b525bdfce4ed5fa02a7ca2ce94fdfbbe9a3703a87b24eeead`) and task-E2E (`a1be4ac8a85e9fd2688976127438aaf08cc83f2848119186e77ce74ae6005118`) releases. Worker candidate-v6 profiles pass Livestack `_profiles` validation and remain mode 0600; both authority candidates pass schema loading at policy revision `benchday-task-environments-e30674875`, with exactly three eligible handlers and 14 forbidden full-E2E/dependency/release handlers. Candidates only; no live config/service changed. The latest ZZOPS human-config status has three queued cargos and two dispatched full-E2E attempts: job `699db5fb1f0d4ea0b2cd1f0574e34057` / attempt `da45bf88df844869ad7cd9e0ec11c514` on `zz-joe-e2e-2`, and job `3aadf401d6724ffe8f199aa26df6740b0` / attempt `ca989189462e492fa54672608fa6e515` on `zz-joe-e2e-1`. Both worker `active.json` records agree on phase `running`; the deploy fence is clear. Rollout remains gated on terminal train state and authority cleanup/result-handoff clearance.

  Rollout-prep recheck (2026-10-06): the candidate Livestack worker source at `3b995e22-task-environments` imports with its bundled dependencies, and `_profiles` validates all three current candidate profiles on zz-joe (Flutter: one cache component; Rust: two; task E2E: 15 npm components plus scratch). Their exact SDK probes pass from `/tmp` with outputs below 8 KiB; Rust uses the installed stable toolchain while HOME/Cargo caches remain under `/tmp`. Candidate-v4 worker configs `/home/ubuntu/.config/livestack-workloads/worker-taskenv-candidate-v4.json` and `worker-2-taskenv-candidate-v4.json` use Benchday source `691eea7656b3e603f7fc0712863201d63868a01a` and current Flutter/Rust/task-E2E handler digests `941477058edabdf98f028094cf9d1981293fa74e092737e1c7b10780cf39a677`, `b32137d9c70a31999f216942e7cf6506affac10f8bd539827c51617d67ea9805` and `6239ed56e16f9780d8965059a8944b366610cbcd2c402621e90b392af836147d`; their payload SHA-256 is `32354759e702193d0ac759edec72317d09e1c664ed5f21b0b730fe2b21f50d42`. They preserve live bases `worker.json` SHA-256 `48d4b6…` and `worker-2.json` SHA-256 `826c77…`; they remain mode 0600 and are not installed. Earlier ZZOPS status showed two dispatched cargos (`tt_b9747407-e899-4953-9728-939a729b797e` completion, `tt_db1ebf05-c937-4a75-b751-0a985d9ff8a7` admission) and three queued. The train reasons say `Harmony running` on `zz-joe-e2e-1` and `zz-joe-e2e-2`; persistent volume/helper setup, authority rollout, worker restart and package activation remain deferred until the execution/cleanup/handoff drain is proven. No live config or service was changed.

  Latest status recheck (2026-10-04): the ZZOPS-backed test-train status read failed during config loading with `Cannot read dedicated zzops file (ENOENT)`, before an authority request. Read-only checks found the ZZOPS config and credential paths unreadable on this host, `zz-joe`, and authority host `xc-tower-ubuntu`. A separate read through the existing Livestack client on `xc-tower-ubuntu` returned two queued unified release jobs and no `running` or `cleanup` rows in its filtered list; both release jobs were left untouched. That client is denied the capabilities route, and its workload list does not expose test-train dispatch or result handoffs. The rollout idle/result-safe gate therefore remains unverified. No configuration, credentials, services, or workers were changed.

  A later repeat of that filtered list showed the shared `benchday.e2e.full.v1` job `0aeb195662384c928a6705a2c146640a` still `running`, with cargo key `benchday-train:tt_7ec0f212-61ec-47aa-9106-eac798272e46`; the two unified release jobs remained queued. Reading that job by ID returned scoped `not found`, so this client cannot expose its attempt/result details. The active full job confirms rollout is not safe now; it was not started or modified for this change.

  Read-only authority config recheck (2026-10-04): `authority.json` exposes `compilation_handlers`, `compilation_policy`, and `handlers`; it does not register `benchday.compilation.rust-check.v1` or `benchday.e2e.task.v1` in the relevant handler maps. This is config-file evidence only, not a live capabilities response. Together with the active shared full job and unavailable official status route, it leaves rollout and admitted execution unsafe/unverified.

  Latest workload-list refresh (2026-10-04): the filtered Livestack read returned two shared `benchday.e2e.full.v1` jobs still `running` (`a14b8bced4534487b554b357212f681c`, cargo key `benchday-train:tt_4626a953-9a14-47b9-9a70-b2b818616aae`; `e41de9ef28f3473f9d363084810c3c0a`, cargo key `benchday-train:tt_1f314fdc-919f-4a5f-973e-56e28f4f4cf7`). Two unified `benchday.release.app.android.v1` jobs remained `queued`; they were left untouched. This confirms the rollout drain gate is closed. The workload list still does not expose test-train results/handoffs. No jobs were submitted or canceled, and no config, service or worker was changed.

  Supported test-train CLI recheck (2026-10-04): from the shared checkout, whose `scripts/test-train.mjs` matches `origin/main`, both `rtk run node scripts/test-train.mjs status --json` and `rtk run node scripts/test-train.mjs submit --change be1d22ac4787de55a17ca31247ef6edb5baad12b` exited 2 before an authority request with `configure: Cannot read dedicated zzops file (ENOENT)`. The selective submit accepted no work. A status response obtained from the Benchday feature worktree came from its older CLI (181 commits behind `origin/main`); its reported `no-evidence` and global gate failure are stale-CLI output and are not accepted as current gate evidence. No job was submitted, canceled or edited during these checks.

  Post-repair recheck (2026-10-04): `npm ci --ignore-scripts` in the pinned ZZOPS runtime worktree installed the lockfile dependencies needed by its CLI; the current Benchday branch's `rtk run node scripts/test-train.mjs status --json` now reaches configuration loading and still exits 2 with `configure: Cannot read dedicated zzops file (ENOENT)`, before an authority request. The authority config still omits Rust compilation and task-E2E registrations; both zz-joe worker configs still omit those handlers and environment profiles. The latest workload list showed one running `benchday.e2e.full.v1` handler job (`1b560eadf431454e9aff5082aad15f9e`), one running unified release-hub job (`bbfebca991db4275838655a97b74dc14`), and two queued Android release jobs; the queued jobs report an active attempt or cleanup. The list does not expose suite selection or result handoffs. All were left untouched. No live config, service, worker or credential was changed.

  ZZOPS cutover update (2026-10-05): the Benchday handoff `docs/handoff/handoff-20261005-zzops-hard-cutover-and-apk.md` reports generation 9 passing `zzops-custody.full-product-hub-stop-redeploy` for merge `cd578729f`, confirming the full-product gate now runs through independent ZZOPS. This does not prove reuse-task-environment handler/profile enrollment or this change's task-specific acceptance. The per-change request for `be1d22ac4` still produced no verifiable verdict: the human config was absent on xc-mac-studio, and the publisher train-view identity's status request failed installed-adapter identity/content verification. Its global gate summary was from an older full run, not the generation-9 verdict. No train, handler, worker or live config was changed for this readback.
- [x] 4.5 Complete Benchday companion agent-interface and changed-assertion acceptance, record alternating cold/warm performance with known/unknown phase data, and archive this change only after all tasks finish. Ledger: evidence references and delivered savings separate from baseline estimates. The later Benchday merge `48c6d99f32a5ec93a87b6375953bec2fa93fed43` has `change_gate.verdict=pass`, `complete=true`, `full=false` for its four derived assertions (train `tt_8e65b4a4-a9f6-4d05-97a6-447f2fedacb1`, producing commit `677851e7982032c2e613e8f7f5a52480d4937910`). This closes that merge's changed-assertion acceptance only; the separate full suite was not run for this change. Alternating cold/warm performance with known/unknown phase data, admitted compiler/cache/timing acceptance, live rollout/readback and archive prerequisites remain open. Follow-up: the real admitted Rust run now supplies one known compiler/phase sample and confirms both Cargo cache components were reused. Queue time dominates, and no controlled same-host cold/warm savings comparison is available; alternating cold/repeat/Dart/native-edit evidence remains open. See evidence/admitted-rust-resume-20261009.md. Stable-path follow-up: Benchday changed-assertion acceptance is PASS/complete/full=false, and the stable-path Rust repeat measured compile phase 28.451 s then 0.173 s with queue/preparation/cleanup phases separated. This one same-source pair lacks host-load sampling and does not close the required alternating cold/repeat/Dart-edit/native-edit benchmark; see evidence/cargo-cache-stable-path-repeat-20261009.md. Gate recheck 2026-10-09 18:31 UTC: watch of durable cargo tt_8355eb73-4f7e-4194-a5b6-b26e99f0c717 is terminal PASS for all four changed assertions, but status --change 48c6d99f32a5ec93a87b6375953bec2fa93fed43 now returns No complete change-bound evidence. Keep this item open until the current change-bound projection is coherent and the remaining benchmark/rollout tasks finish. Reattachment 2026-10-09: fresh `zzops train status --change 976f344600eea42213ff91d4f2fab317e9af4965` reports `change_gate.verdict=pass`, `complete=true`, `full=false` for the single derived assertion `fleet-workload.task-environment-runtime-freshness` (train `tt_aaff49f4-15b2-4595-98a3-5ee72977aebd`, producing commit `ed57136deaf7a5955776df796ad3b85bebd6c168`). This closes only that named merge gate; benchmark, rollout, rollback, and archive remain open. See `evidence/benchday-changed-assertion-976f-20261009.md`. Current follow-up for earlier merge `48c6d99f32a5ec93a87b6375953bec2fa93fed43`: `status --change` still returns missing / `No complete change-bound evidence`; the PASS for `976f...` is a separate merge and does not resolve it. Keep this item open. Recheck 2026-10-10: fresh ZZOPS `status --change 48c6d99f32a5ec93a87b6375953bec2fa93fed43` again reports `complete=true`, `full=false`, PASS for all four scoped task-environment assertions (train `tt_3dfc7a85-b40c-4b55-8ec8-52f69ba47373`). The alternating admitted Flutter/Rust measurements are recorded in `evidence/alternating-benchmark-20261010.md`; rollout and other task rows remain open, so this change is not archived.

  Disposable rollback compatibility check passed 1/1 in test commit `886f0067` against Livestack implementation `8bd2c43`: with environment enrollment disabled, a legacy schema-1 `full.v1` request is accepted and a full-handler environment reference is refused. This verifies private authority request compatibility only; installed-worker rollback, full-test execution, changed-assertion evidence and performance measurement remain open.

  Current rollout-prep recheck (2026-10-06): the Benchday companion source was refreshed to `1d5675c0ae978ba5c2c769a1e2895e581534c7c4`. The filtered handler payload SHA-256 is `6f75564b9198f07aa2ce91a5143601a0c75a764604a57b7e468117579d1d98e4` and includes only Flutter, Rust and task-E2E packages. Worker candidate-v9 configs are mode 0600, preserve the unchanged live base hashes (`48d4b6…` and `5fafc8…`), and the current Livestack candidate `TaskEnvironmentStore._profiles` validator accepts all three profiles on both workers. Authority base/final candidates at revision `benchday-task-environments-1d5675c0a` pass `load_config()`; the final candidate classifies three eligible and 14 forbidden handlers. Live worker and authority config hashes are unchanged. ZZOPS currently reports one dispatched completion cargo on `zz-joe-e2e-1` (job `72ca2296adbe4093a1970721858c2d63`, attempt `dcbf8dfcb365406592ece6adeed470c2`); `pending_cargo: 0` does not clear the active worker. Persistent storage provisioning, worker/authority rollout, package activation and admitted acceptance remain deferred until the attempt and result handoff are terminal.

  Gate-containment readback (2026-10-06): the dispatched ZZOPS full cargo selects `full`, reports `coverage: containing-full`, and runs commit `e83027e32a9188c2eef6dec84a52e597c681c294`, which contains Benchday feature commit `511edc795f9e52a8541e7406af4e73d00b331585`. Its change-bound status remains incomplete while dispatched; `watch` returns 404 for the listed cargo and is not treated as terminal evidence. No duplicate gate was submitted. Joe storage inspection found no spare LVM extents (`zz-joe-vg VFree=0`); root has 1,057,614,135,296 bytes available, and the planned environment mount is absent. This records the physical layout for the bounded-volume provisioning step; no disk or mount was changed.

  Worker drain refresh (2026-10-06): Joe's authoritative `active.json` records show full E2E job `72ca2296adbe4093a1970721858c2d63` / attempt `dcbf8dfcb365406592ece6adeed470c2` on `zz-joe-e2e-1` and a separate Flutter compile `a95329fdfc0a46598c02b2213e183001` / attempt `1644f0e51bdd4199a6d94f7457f1e91a` on `zz-joe-e2e-2`; process trees were present for both attempts. ZZOPS reports the first cargo but not the second direct workload. Both slots are occupied and untouched; do not restart/provision until both finish and their cleanup handoffs are confirmed.

  Subsequent drain check (2026-10-06): the direct worker-2 Flutter jobs `a95329fdfc0a46598c02b2213e183001` / `1644f0e51bdd4199a6d94f7457f1e91a` and `67fdb065f3ec4ad5bc6d24f100e0fcb5` / `b121c03a677f4028870583c95096ab80` are terminal failed with ended attempts per the scoped Livestack `get` readback; worker-2's active record and attempt process are now absent. The ZZOPS full E2E job on worker 1 is still `running`, so host-level storage provisioning and authority/worker rollout remain deferred.

  Latest drain check (2026-10-06): both Joe workers are occupied again by ZZOPS-owned E2E. Worker 1 runs full completion cargo `tt_91f24c4c-537e-4f17-a101-18749e3cc58d`, covering Benchday commit `e83027e32a9188c2eef6dec84a52e597c681c294` that contains feature commit `511edc795f9e52a8541e7406af4e73d00b331585`. Worker 2 runs admission cargo `tt_a1998fa5-2ee4-4b53-ad76-bb781df94b8c` for `context-storage.segment-roundtrip` at `d5a673cfe6aab4bab9eb216d975392f4a1a2d866`; it also reports `coverage: containing-full`. Both current worker records are phase `running`; no worker capacity is free for rollout.

  Latest source-aligned payload refresh (2026-10-06): Benchday was fast-forwarded to `f3c53517df6a412849367c61999ad2c77d01d580`; the copied/extracted handler payload SHA-256 is `a8e082800b8461006223ce830aa27680eea9e5d0474e12f541d0db88284ec8ae` and contains only Flutter, Rust and task-E2E packages at release digests `bb2c23fddccfa26634400d98d3d1e78f0d725b18bdd1db93b53b92f34356f953`, `cf6498bb6b54842dd42717e5745b66be5182bdf9c41ea5ae3e6522cd33b24d78`, and `2fc3bbe9f1f0cc8744cd8119d2208f8e37f6df37f534fb329a168290f1e63ed8`. Worker candidate-v10 configs remain mode 0600 and validate all three profiles; their bounds are 32 GiB per replica, 128 GiB per owner and 127 GiB aggregate for a 128 GiB filesystem with 1 GiB reserve. Authority candidates validate at `benchday-task-environments-f3c53517d`, classifying three eligible and 14 forbidden handler purposes. No live config or service was changed.

  Environment volume and current drain readback (2026-10-06): the persistent Joe ext4+prjquota volume is now installed and the helper/store/quota controls described in task 3.1 passed; it is provisioned but not enrolled in either live worker. The Benchday task worktree has since advanced to `c22ea16421c7544953d50b10e3de4954863f581f`; its filtered Flutter/Rust/task-E2E payload is staged at `/tmp/benchday-taskenv-stage-payload-trimmed-c22ea1642.tar.gz` (SHA-256 `541d8a8d02d4da4030bf35ee979f46c6056f565ca426ddf12e3ec21758ee695c`). Current ZZOPS status has two dispatched cargos: completion `tt_11848adb-4362-4f17-922e-0723a0dd8f01` / job `72ca2296adbe4093a1970721858c2d63` on worker 2 and admission `tt_bd24190c-3df0-462b-8198-2c3e314c003a` / job `9dd93d7a6b35466a93108e0293cfec60` on worker 1. Both still report `running`; no worker/authority rollout or restart is safe until these independent cargos reach terminal state and cleanup/result handoff is confirmed.

  Worker-runtime staging and drain refresh (2026-10-06): Livestack commit `90a0c8cf` is pushed to `origin/main`. Its 14 task-environment runtime files (archive SHA-256 `6c4570cd3e31db906990f1f2c216e5f42968ba345d38c08fc1b20a70fab9deb5`) are overlaid onto additive candidate releases for both Joe workers; import probes resolve `worker` and `task_environments` from each candidate path. Existing release directories, live `worker.json`/`worker-2.json` (SHA-256 `48d4b6…` / `5fafc8…`), drop-ins, and service processes are unchanged. Latest ZZOPS status reports completion cargo `tt_11848adb-4362-4f17-922e-0723a0dd8f01` / job `72ca2296adbe4093a1970721858c2d63` dispatched; the admission cargo is now `tt_d3fdca02-7e2b-4910-aec4-a19ca3f36297` with `Harmony submission pending`, while worker 1's authoritative active record shows full-E2E job `fd70adbd12a84f29800f35c81299701d` / attempt `75afbf0572ec4b69a58f6bb0e2f628b5` for that cargo running. Worker 2 still shows full-E2E job `72ca2296adbe4093a1970721858c2d63` / attempt `292d759ba21f4dbda3c167f7db1c6a43`. Both slots remain occupied; no restart or live config activation occurred.

  Authority-side candidate staging (2026-10-06): overlaid the same 14 source files into a new immutable authority release ending `+taskenv90a0c8cf`. Import probes resolve service/store/placement/worker/task-environment modules from it. New mode-0600 base and final candidates `authority.task-environments-base-candidate-c22.json` and `authority.task-environments-candidate-c22.json` both pass the staged release's `load_config()`; the final policy is `benchday-task-environments-c22ea164`, with 3 development/task-E2E handlers eligible and 14 full-E2E/release handlers forbidden. The live authority still has no `environment_handlers`, uses policy revision `benchday-e2e-handler-release-burst-20261005`, and remains on its previous release/PID 913563. No active config, drop-in or service was changed.

Current storage-bound readback (2026-10-06 12:31 UTC): Joe's mounted environment volume is `/dev/loop1`, ext4 with `prjquota`, totaling 134,145,380,352 bytes. With the configured 1 GiB reserve, the worker's `TaskEnvironmentStore` runtime clamp yields an effective aggregate ceiling of 133,071,638,528 bytes (123.933 GiB). Created candidate-v11 configs for both workers, derived from v10 with only `task_environments.max_total_bytes` set to this exact ceiling; both parse as JSON and remain mode 0600. The live configs/services are unchanged. Worker 1 still has shared ZZOPS full completion active; worker 2 is idle. Do not restart or activate until the train/result handoff and drain conditions are terminal.

Drain recheck (2026-10-06 12:37 UTC): Joe worker 1 now has a new ZZOPS admission attempt in `preparing` (`84f825b263a846939aefdcaa5ef17f60` / `635bffe3cb344c6fbcf738219e919b18`, owner `zzops-benchday-test-train`); worker 2 is idle. ZZOPS still reports this task's changed-assertion cargo and the new train dispatched, with another train dispatch-pending. Keep the runtime/config rollout staged only until this shared attempt and its result/cleanup handoff reach a terminal state.

Authority reload boundary confirmed (2026-10-06): `ReloadableConfig` intentionally re-reads principals, installed handler IDs and handler-release policy; it does not load `environment_handlers`, which is read when `WorkloadStore` is constructed. The task-environment authority candidate therefore needs a process restart to activate. Current ZZOPS work is still active, so runtime/config rollout remains deferred until the authority can be restarted in a clean window.

ZZOPS transport recheck (2026-10-06 12:56 UTC): the task admission train remains `dispatched`; status now reports `Harmony reporting unavailable: read ECONNRESET` on repeated reads, while its watch command has no terminal response. This remains transport uncertainty, not failure evidence. Another admission is active on Joe worker 1, so no authority/worker rollout or rollback was made.

Changed-assertion gate/drain update (2026-10-06 13:08 UTC): the Benchday admission cargo `tt_27584fd2-406d-4f73-b340-a2c9cc2c4e38` is terminal `fail` with four selected task-environment assertions passing and `fleet-workload.remote-flutter-caller-refuses-local-builds` failing. The supported ZZOPS status/watch response does not expose the failing fixture output; the same guarded Flutter and Rust fixtures pass locally, so cause remains unconfirmed. Benchday `status --change be1d22ac4787de55a17ca31247ef6edb5baad12b` returns `No complete change-bound evidence`. Latest global ZZOPS status has four pending cargos and two trains in flight; defer retry and live rollout until capacity and the authority/worker drain are clear. No live worker or authority config/service changed.

Current-source and drain refresh (2026-10-06 13:21 UTC): this task worktree fast-forwarded to `3d7e43a9`, which includes Livestack main changes after the earlier `90a0c8cf` staged runtime, including current worker result-size handling and handler-capacity lookup. `openspec validate reuse-task-environments --type change --json` passes. The current Benchday guarded Flutter and Rust caller fixtures pass locally in 0.56 s and 0.12 s. ZZOPS shows three dispatched trains (one on `xc-win-1-wsl`, one on `zz-joe-e2e-1`, and one on `zz-joe-e2e-2`); do not retry or restart while they run. Rebuild and verify worker/authority candidates from the current source before rollout so the older staged candidate cannot omit these mainline changes. Live configs and processes remain unchanged.

Current-source and queue refresh (2026-10-06 15:37 UTC): the Livestack task worktree is at current `origin/main` `b283a24dec042b8850a9137ca0e1b35a89523522`; OpenSpec validation passed. The current Benchday source is `6ec0d56a743928f7d2d8267cef15c87595962fa0`, with fresh local Flutter, Rust, and task-E2E release candidates documented in the Benchday change ledger. No release was staged or activated, and the previously prepared worker/authority candidates remain inactive. ZZOPS reports the task's changed-assertion cargo still dispatched on `xc-win-1-wsl`, shared full completion active on Joe1, another admission active on Joe2, and one admission waiting for capacity. Keep service activation/restart deferred until the task train, result handoff, and all worker cleanup reach terminal readback.

Worker release candidate refresh (2026-10-06 15:47 UTC): built current-main Livestack release `c1f86e4e12204b666edb2323bf498efbed67d94e` to `/tmp/livestack-worker-release-c1f86e4`; its content hash is `f31c09a338966ed4b4f576813ae6da8b4a183f8f6f6340d76354a7c22f0c5a91` (223 files), confirmed by the read-only hash command. Read-only comparisons to both deployed Joe E2E worker releases are different: the candidate contains the task-environment modules absent from the 198/196-file deployments. The comparison flags deployed `worker.py` (and Joe1 `client.py`) as hand-edited; `node-py/docs/worker-release-rollout.md` records that the current mainline release is the supported superset. Candidate only: no package staging, config edit, or worker restart occurred. ZZOPS at 15:47 still showed the task assertion running on WSL, a full completion on Joe1, and an admission on Joe2; the task cargo remained `waiting-containment` with no verdict. Keep activation deferred through terminal train and cleanup readback.


Current-source rollout staging (2026-10-06 19:06 UTC): Livestack task worktree is at `a211db84ef8cd9977aff8e0d7433210e450c6cb7`. Built immutable release `livestack-a211db84` (223 files; content hash `f31c09a338966ed4b4f576813ae6da8b4a183f8f6f6340d76354a7c22f0c5a91`) and staged it under new versioned directories on the authority and Joe host; the supported release verifier reports both byte-identical to the build. No service pointer, drop-in, live config or process changed. Authority candidate v13 (mode 0600, SHA-256 `7d74450a48cc8ad5cae1b644689101fd82427b8edf7684d6a2eabf7b553cf998`) was derived from the current live config with only task-environment limits/policy, two compilation-handler classifications, the two owner handler grants and a new policy revision; `AuthorityConfig`, `Limits`, principals, compilation policy, handler registry parsing and a temporary `WorkloadStore` all validate from the staged release. It retains `terminal_seconds`, authorizes three development/task-E2E handlers and explicitly forbids 14 full-E2E/release handlers. Joe worker config candidates v12 (SHA-256 `ee37dd09c7cbcaa73581fff09c18c03332b15689777e6d9788dd48084a35060f` and `c48b82226ebd775b9731fcd050061758f9854ce06e040c2fd833fdaf6c9d315f`) are mode 0600, preserve the live 8 GiB transfer bounds and add `/usr/bin/python3` plus the three bounded profiles. The three new Benchday package digests are staged in the handler registry but are not defaults; generation remains 16. Worker reports still show the old effective generation and prior `handler_runtime_not_installed: python3` activation failures, which the candidate runtime config addresses. At this snapshot ZZOPS has full completion active on Joe worker 1, admission active on Joe worker 2, admission active on WSL and queued work. No authority/worker config was activated and no service restarted.

Drain follow-up (2026-10-06 19:09 UTC): the watched unrelated Joe-worker-2 admission ended fail on `client-performance.frozen-wave-and-new-output`. The next ZZOPS status still shows a full completion on Joe worker 1, new admissions on Joe worker 2 and WSL, one pending submission and queued work. This is not task-environment acceptance evidence. Keep the staged release/config candidates inactive until all attempts, cleanup and result handoffs are terminal.

Current-main rollout preparation (2026-10-06): the Livestack task worktree is at `origin/main` `1d34fcd37a0e80ea0c722b70d56ad9de360ba3b4`. A clean worker release built from it has the same content hash as existing immutable candidate `livestack-a211db84` (`f31c09a338966ed4b4f576813ae6da8b4a183f8f6f6340d76354a7c22f0c5a91`, 223 files); `verify` reports identical copies on authority and Joe. No service pointer or process changed. The refreshed filtered Benchday handler packages from source `01d8310588862aebbff231c8385baa492a620ad4` / ZZOPS pin `66417af0c69e5d68c727786d5479959f4a2854c6` are staged only: Flutter `93359f2b860dc974cac4441c97f8ad4bdc0f3559093cd8d9d644aa5d2066eab6`, Rust `355d0665fc3d05a8b4b3befe93e4673e182a5501f8fbb288d2204f042833e10c`, task-E2E `81930d61951ab6c913b676e9a29dec514d3e4429cfcbe9f4720ae25ff50c8d34`; registry generation 16 and all defaults remain unchanged.

Derived authority candidate v14 (`authority.task-environments-candidate-v14-01d831.json`, mode 0600, SHA-256 `cecf5a8f7269af0327cf80c3318bf9451e18a322d15f11c638a9ab4ad3c3093f`) from the live config and previously validated environment delta, preserving release-policy revision `benchday-task-environments-5cf9c2175` and `terminal_seconds=259200`. Staged Livestack code validates its 28 principals, 35 handlers, limits, compilation classes, handler policy and temporary `WorkloadStore`; environment scope is 3 eligible handlers and 14 forbidden full-E2E/release handlers. Joe's mode-0600 worker candidates v12 validate all three profiles through current `TaskEnvironmentStore._profiles` and retain the live 8 GiB transfer caps. The live configs, defaults and service pointers were not changed.

Authority read-only database readback (2026-10-06): 3 attempts are running on `zz-joe`, 1 attempt is in cleanup on `xc-tower-ubuntu`, and 5 jobs are queued; the live database does not yet have `task_environments`. The ZZOPS snapshot also has shared work dispatched and pending. No authority/worker activation or restart, new task-environment workload, or train submission occurred. Keep task 4.4/4.5 and rollout acceptance open until the active attempts and cleanup/result handoffs are terminal and the live worker/authority acceptance can run.

Latest runtime and scheduler refresh (2026-10-06): Livestack remains at `origin/main` `d632d7bf9740926cf98766803411d470fa8e343e`; the newly built source is byte-identical to staged runtime hash `f31c09a338966ed4b4f576813ae6da8b4a183f8f6f6340d76354a7c22f0c5a91`. Benchday's current source is `29c6e5e8b8d6e40ef079d110d53e29148fc3abb4` with ZZOPS pin `1f957cbe248cbd53a1f91e0965cd3d339595bcc0`; current Flutter/Rust/task-E2E releases are staged in registry generation 16, with defaults unchanged and no full-E2E/release package. Authority v14 and Joe worker v12 remain inactive candidates. Latest ZZOPS readback reports unrelated full completion `tt_56d21547-8985-4df8-8797-5a89388b5914` active on Joe worker 1, admission `tt_5640cff1-4ab9-4cc5-8086-06e733e25393` active on Joe worker 2, plus three queued cargos. Do not restart or activate while attempts, queued placements, cleanup or result handoffs remain. This is a shared infrastructure drain, not missing access; no live config, service pointer or process changed.

Current source and active state (2026-10-06): Livestack runtime remains source `d632d7bf9740926cf98766803411d470fa8e343e`, byte-identical to staged runtime hash `f31c09a338966ed4b4f576813ae6da8b4a183f8f6f6340d76354a7c22f0c5a91`. Benchday packages were rebuilt from `19ce91f730f9cb5163b15f3c7074fcb4d0adca60` and ZZOPS pin `54625c2c7f5a1bd50f57538435ce3a94fa71567e`; only Flutter `966d8401dbe1e9af300e305d895d6347389a636ff45fbf26ab248d83ae561b25`, Rust `4d03fa1cf5f230fe1247493425ccf7227a6c6cadc97da828e4e62cacdbf4bade` and task-E2E `a2889f7fda229993c844402ab6091ce3b40b71eab389f1c42049896f42726877` were staged in authority registry generation 16. Status confirms each is non-default; all defaults remain unchanged, and Joe workers report effective generation 10. No full-E2E or release package was staged. The current authority DB read shows 3 running attempts, 1 cleanup attempt and 4 queued jobs; `task_environments` is absent. Latest ZZOPS readback shows two running Joe-worker trains, two more queued because both slots hold active attempts/cleanup and WSL is under failure avoidance, three waiting cargos and zero fresh runners. Keep authority/worker activation and restart deferred; no task-specific attempt has run. No live config, default, service pointer or process changed.


Current-main worker candidate and authority drain (2026-10-06 20:22 EDT / 2026-10-07 00:22 UTC): refreshed this task worktree to Livestack origin/main 7b2e338e024d1665d55ac09345b0ba47b4dc165b. The source adds opt-in PSI CPU admission and plain-Python worker startup validation; the rebuilt immutable candidate has 224 files and content hash f0aa84ac6624433d053377b811430c54a7bd4528d0bb46eb2b3f803baf39617d. It is built locally only; no worker release directory, unit pointer, worker config or process was changed. Read-only inspection of the live authority host reports checkout 35edfd075e16532feb684458a1178b3b19bedfc8, 46 commits behind its recorded origin/main, and no task_environments table in the live database. The authority currently has 4 running attempts, 1 cleanup attempt and 2 queued jobs. ZZOPS reports 3 dispatched trains, 1 dispatch-pending train, 1 pending cargo and 1 waiting cargo. Benchday's current Flutter, Rust and task-E2E packages were staged non-default in handler registry generation 16; the existing default map remains unchanged. Do not restart or activate while attempts, cleanup and result handoffs remain. No task-environment request or admitted execution has occurred; environment enrollment, real receipts/cleanup, alternating timing measurements, live rollout and rollback acceptance remain open.


Immutable runtime staging and active-state refresh (2026-10-06 20:38 EDT / 2026-10-07 00:38 UTC): built the 224-file Livestack worker release from current main 7b2e338e024d1665d55ac09345b0ba47b4dc165b (content hash f0aa84ac6624433d053377b811430c54a7bd4528d0bb46eb2b3f803baf39617d) and copied it under the new immutable path /home/ubuntu/.local/share/livestack-workload-releases/livestack-7b2e338e on authority 100.64.0.18 and zz-joe 100.64.0.24. The supported verifier reports both remote copies IDENTICAL to the build. This only staged candidate files: no worker/authority config, unit pointer or process changed. Benchday packages were refreshed from 3a4bff9857ba81157c2d16627f75fced6138da25 and staged non-default in registry generation 16; the existing default map is unchanged. ZZOPS currently reports pending 1, waiting 1, four dispatched trains, one dispatch-pending train and zero fresh runners. The authority DB read at 00:37 UTC showed 3 running attempts, 1 cleanup attempt, 2 queued jobs and no task_environments table. No task-environment request or admitted execution has run. Do not activate/restart until all shared attempts, cleanup and result handoffs are terminal; real environment acceptance, timing comparisons, live rollout and rollback evidence remain open.


Drain recheck (2026-10-06 20:42 EDT / 2026-10-07 00:42 UTC): the live authority remains without the task_environments table and has 3 running attempts, 1 cleanup attempt and 2 queued jobs. The latest ZZOPS status shows 2 pending cargos, 2 waiting, zero fresh runners, 3 dispatched trains, 1 dispatch-pending train and 1 boarding train. The newly staged release livestack-7b2e338e remains only an immutable candidate on authority and zz-joe; no pointer, config or process was changed. Wait for a verified drain and authority upgrade before any live task-environment request.

Rootless-Docker cleanup acceptance (2026-10-07): added `node-py/tests/test_workload_task_environment_docker.py`, which drives a real rootless Docker child through lease expiry and checks cgroup, container, socket, replica-advertisement and resource-claim cleanup. `python3 -m py_compile` and `git diff --check` passed. The runtime integration test has not run: the inspected light hosts lack RootlessKit/slirp4netns, and the admitted zz-joe workers currently carry shared jobs. Keep Livestack task 3.3 open until this test passes on a suitable idle host and live worker readback confirms cleanup.

Current-main rollout preparation (2026-10-07): fetched Livestack `origin/main` at `9fddd79576eef54b02942bb35ed4633a91be8af6` (the earlier local tracking ref was seven commits behind) and fast-forwarded this task worktree. The active authority is already running that exact source; its 230-file worker release hash is `6e26dc03002d5ff715ff903802e9b9955c1262100ddf71f0c1d6600085296c44`, and the staged zz-joe release verifies `IDENTICAL`. Read-only live capability reports versions 1/2/3 and environment API v1, but no eligible or forbidden environment handlers. The live DB has the task-environment tables with zero environments/replicas. Current active shared attempts are full-E2E work on `zz-joe-e2e-1`, `zz-joe-e2e-2`, and `xc-win-1-wsl`, plus cleanup on `xc-tower-e2e-1`; do not run the real rootless-container acceptance while the two zz-joe slots are busy.

The ext4 project-quota positive control passed on zz-joe using the current source: `ext4+prjquota`, overflow returned `EDQUOT`, and a second project retained its independent 8 MiB quota. The shared environment root is root-owned, group `ubuntu`, mode `01770`; its mount is `prjquota`. Fresh mode-0600 worker candidates v13 (`8ad4fc71cbd696766afcc03361343e66ed98b7257b15562b09216540d23d0b25` and `a42605bbefaedd2f5ebaf16dc679ebce276600f31fdb65a119a77519deea439e`) were derived from current live configs, preserving their latest admission, labels, workspace, handlers, and 8 GiB transfer bounds while adding only Rust/task-E2E handlers, the three task profiles, and bundle labels for Benchday commit `909844c7`. All three real profile probes passed on both candidates with no profile errors. They remain inactive.

Authority candidate v16 (`authority.task-environments-candidate-v16-909844.json`, SHA-256 `12512666b15abe72a49af4961916ffc5c131e6fe59f24656c0bdb498ecd90813`, mode 0600) was rebuilt additively from the live config and validated against the active source: all 32 principals and 36 handlers are preserved; it adds the two owner grants, Rust/task-E2E compilation classes, seven environment limits, and 17 environment-purpose policies. Config, limits, compilation policy, and temporary-store validation all pass. Current Benchday bundles from `909844c7` were staged only for Flutter (`949e23a11bbfb3a2aa452c965ba80289fb4bce9134985973f7052ddd7bb72a62`), Rust (`62631798466bf63e91eb1e064ac09cde4d05531c151edacf2c503e403860fec0`), and task-E2E (`11392806b753298f0639668972a0c7af3e07e8f4798c473497f95c0f6dde5675`); registry defaults remain unchanged. No task-environment job, worker/authority restart, config activation, full-suite train, or publish was initiated. The authority host root filesystem is at 95%, so keep the database-backed authority deployment deferred until the shared work drains and its backup is safe. Livestack tasks 3.2, 3.3, 3.5, 4.1, 4.4, and 4.5 remain open; no live reuse receipt or timing comparison exists yet.

Live-state recheck (2026-10-07): the user-level authority service is active on `100.64.0.18:8810` and its database contains the task-environment tables, with 0 environments and 0 replicas. Authenticated `benchday-owner` capabilities report versions 1/2/3 and environment API v1, but no environment-eligible or forbidden handlers. There are 3 running full-suite attempts (`zz-joe-e2e-1`, `zz-joe-e2e-2`, `xc-win-1-wsl`) and 1 tower cleanup attempt. ZZOPS reports 2 shared cargos waiting and 0 fresh runners. The authority root is at 95% usage (90 GiB free); its DB is 8 MiB plus a 117 KiB WAL. Do not run rootless-container acceptance, activate worker candidates, or restart the authority until the active attempts and cleanup are terminal. No task-environment workload or service/config change was made.

Drain recheck after the brief wait (2026-10-07): the `zz-joe-e2e-1` attempt ended; `zz-joe-e2e-2` and `xc-win-1-wsl` remain active, as does tower cleanup. ZZOPS now has 4 shared cargos waiting and 0 fresh runners. The authority still reports 0 environment rows/replicas. No task-specific workload, activation, or service/config change occurred; keep the real worker acceptance and rollout deferred.

Candidate review and drain recheck (2026-10-07 23:20 UTC): fixed `SystemdExecutor.stop()` to use the manager recorded by inspection and added manager-discovery/stop controls. Moved native `no_new_privs` setup into the single-threaded frontend immediately before it starts task code, avoiding a Python `preexec_fn`; corrected the rootless Docker test's failure/lease branch indentation. Embedded-handler syntax, manual system-manager selection/stop controls, the real `PR_SET_NO_NEW_PRIVS` `/proc` positive control, `compileall`, and `git diff --check` pass. The focused pytest controls could not run on this local Python 3.14 host because pytest is absent and unavailable in its offline cache; the admitted-host acceptance has not run. ZZOPS has zero pending/waiting cargos but four dispatched trains, three on Joe; Joe load is 24.27 and I/O PSI is `some avg10=46.84`, `full avg10=43.50`. No release was staged or activated, and no live task-environment job was submitted. Keep tasks 3.2–3.5 and 4.1–4.5 open until candidate integration, real admitted receipts/timings, worker policy/cleanup readback, rollback, and Benchday acceptance are verified.

Dispatch recheck (2026-10-07 23:22 UTC): ZZOPS now reports 6 dispatched trains (5 on Joe e2e workers, 1 on WSL), 3 pending cargos, and a dispatch-pending train because the circuit is open at 6 outstanding. Joe load is 5.48, but I/O PSI remains `some avg10=10.93` / `full avg10=10.04` while five shared jobs run there. No task environment ran and no worker/authority release changed; retain the deferral until the shared jobs clear.

Authority reload and capacity recheck (2026-10-07 23:31 UTC): `ReloadableConfig` now supports safe SIGHUP replacement of `environment_handlers`; omission preserves the installed policy and `{}` disables enrollment. A real isolated authority/service-process test suite passed 32/32 on lappy-bellinzona (Python 3.13.15, pytest 9.1.1), covering enablement, omission, invalid-config atomic refusal and rollback/caller refusal. Temporary staging was removed. No live config, service or worker was changed; the current authority release still needs this code upgrade before SIGHUP can apply task policy. ZZOPS now has 6 dispatched trains, 4 waiting cargos and 2 dispatch-pending circuit-open trains. Joe load is 13.92 with I/O PSI `some avg10=5.51` / `full avg10=4.68`; five shared trains remain on Joe. Authority root usage is 96% (82 GiB free), so its release upgrade remains deferred pending drain and safe database backup. Rootless Docker/compiler acceptance and all live rollout/readback remain open.

Worker-control smoke (2026-10-07 23:32 UTC): on the same low-load lappy host, `test_hostview.py` passed 7/7 and the two system-manager inspect/stop controls in `test_workload_supervision.py` passed 2/2 (10 other supervision tests deselected). Temporary staging was removed. This is hostview and mocked manager-routing coverage, not rootless Docker acceptance.

Current shared-capacity recheck (2026-10-07 23:32 UTC): ZZOPS reports 6 dispatched trains, 6 waiting cargos and 3 circuit-open dispatch-pending trains. Joe load is 9.84; I/O PSI has eased to `some avg10=2.10` / `full avg10=1.66`, but shared trains remain active there. Authority root remains at 96% (81 GiB free). No task workload, worker rollout or authority change occurred. The two low-load lappy test runs validate reload and manager controls only; do not treat them as rootless/container/compiler acceptance.


Capacity/backup checkpoint (2026-10-07 23:34 UTC): one read-only ZZOPS status read reports 6 dispatched trains, 3 dispatch-pending trains, and 4 queued cargos. No task-environment acceptance was submitted. Keep the admitted rootless/compiler sequence deferred until the shared work drains. Review of Livestack's supported authority deployment procedure found the concrete backup step in `tools/deploy-authority-release.sh`: private SQLite `.backup`, `pragma integrity_check`, and a mode-0600 config copy before the drop-in switch. The backup has not been taken and no service/config/worker state changed. Static review remains green (`git diff --check`, `python3 -m compileall -q node-py/livestack_node`).


Capacity recheck (2026-10-07 23:35 UTC): ZZOPS status now shows 6 dispatched trains, 2 dispatch-pending trains, and 6 queued cargos. The shared lane remains occupied; no task-environment workload was submitted. The one-time authority upgrade remains pending its full idle window; the supported release script's verified SQLite/config backup step is identified but not run.


Spec validation checkpoint (2026-10-07): `openspec validate reuse-task-environments` passes; `openspec validate --specs` passes all 10 Livestack specs (existing advisory warnings only). `git diff --check` passes after the ledger updates.


Authority preflight and backup-path fix (2026-10-07 23:41 UTC): read-only inspection of the live authority shows 6 running jobs, 1 cleanup attempt, 0 task environments, and an active authority; `/` is 96% used with about 81 GiB free. The host has Python SQLite 3.46.1 but no `sqlite3` executable, so the existing deployment script's database-backup step would abort. Updated `tools/deploy-authority-release.sh` to make a read-only-source SQLite online backup with Python's standard-library `Connection.backup`, require `PRAGMA integrity_check = ok`, chmod the resulting DB 0600, and remove a failed partial backup. `bash -n`, `git diff --check`, and an execution of the exact embedded backup code against a disposable WAL-mode database pass (contents preserved, integrity ok, mode 0600). No live backup, authority change, worker rollout, or task-environment activation occurred.


Backup-path fix landed (2026-10-07): commit `19dc80eb` (`Use Python SQLite backup for authority release`) was pushed to Livestack `origin/main`; it changes only `tools/deploy-authority-release.sh`. Remote head verification returned `19dc80eb`. The authority was not restarted, and this commit does not change the worker runtime.


Fenced-rollout path inspection (2026-10-07): ZZOPS has no Livestack-bound release graph (`zzops release run ls --app livestack` refuses the absent/ambiguous application binding), and `zzops deploy` is for website targets. The ZZOPS self-hosting runbook does expose `zzops-admin fence-hold|fence-drain|fence-release`; omitting `app` holds/drains all applications in that private service config. For the eventual authority upgrade, use that global fence with abort-on-drain-timeout around the existing Livestack release procedure, then verify zero authority attempts/cleanup before restart and release the fence afterward. No fence or deploy was invoked; the active attempts listed above remain.


Live capacity recheck (2026-10-07 23:45 UTC): ZZOPS still reports 6 dispatched trains, 3 boarding, 2 dispatch-pending, and 6 queued cargos. The authority database independently reports 6 running attempts (five Joe E2E workers and one WSL worker), 1 tower cleanup attempt, and 0 task environments. No eligible worker is idle; no environment workload or live service operation was started.


Candidate/runtime recheck (2026-10-07 23:52 UTC): ZZOPS doctor is healthy; Benchday train status reports 7 pending cargos, all 7 waiting and 0 stranded. The authority database now has 5 running attempts, 1 cleanup attempt, 1 queued job, and no task environments or replicas. No environment workload was submitted. The authority runs Python 3.14.4; its active release `livestack-9fddd795` uses a 204-file `_deps` directory (8,908,257 bytes; manifest SHA-256 `5a8eee334729017ea1950d5fe31f0982cad199da426d91ea64cb3ed8422147a3`, including pydantic 2.12.5). Copied that immutable dependency directory into the local temporary candidate built from `887fe3e496f4dbf9a3d9b8f1a46dd01cee069a44`, verified the dependency manifest matches byte-for-byte, then ran `tools/check-authority-release.py` with local `/usr/bin/python3` 3.14.7: static, boot, worker registration and job round-trip all PASS. This is candidate validation, not a live authority test. No live service/config/worker state changed; authority upgrade, worker acceptance and environment enrollment remain open until a safe idle/fenced window.

Timing-failure control (2026-10-07): Benchday's `RustSubmissionTest.test_phase_timing_write_failure_propagates_before_compilation` passed 1/1 on the current Benchday source against this Livestack candidate. The forced trace write error propagates before compilation and leaves no receipt. The admitted compiler positive control and end-to-end ledger joins remain open.

Live authority recheck (2026-10-08 00:05 UTC): service is active; its configured handlers include Flutter/Rust compilation and task E2E, but `environment_handlers` is empty. SQLite reports 6 running attempts, 1 cleanup attempt, and 0 task environments/replicas. No task environment job, restart, reload, config edit or worker roll occurred. Keep authority and worker rollout deferred until the shared attempts and cleanup drain.

Local source-regression refresh (2026-10-08): on `lappy-bellinzona`, the current worktree's targeted `test_task_environments.py` coverage for complete mirror reconciliation/lockfile invalidation, incompatible profile relocation, toolchain probe replacement, source integrity, internal/escaping symlinks and captured-source aliases passed 6/6 with Python 3.13.15. This refreshes fixture evidence only; admitted compiler/ABI invalidation and real toolchain reuse remain open.

Safe-rollout gate recheck (2026-10-08 00:10 UTC): `zzops doctor` is healthy and the Benchday status read shows 5 cargos waiting. A read-only authority check finds the service active with 6 running attempts, 1 cleanup attempt, and 0 task environments/replicas; authority root is 96% used with 78 GiB free. The required zero-attempt/zero-cleanup window is not present, so no environment job, fence, restart, config change or worker rollout was started.

Current-source fixture/worker regression (2026-10-08): on `lappy-bellinzona`, the full `test_task_environments.py` and `test_workload_environments.py` modules plus `test_worker_reuses_task_environment_across_captured_source_edits` passed 40/40 in 12.02 seconds with Python 3.13.15. The worker case used a private authority and local systemd worker; it does not prove the admitted compiler or rootless Docker path.

Exact-attempt poll (2026-10-08 00:13 UTC): all seven authority attempts observed at 00:10 remain present with the same states (six running, one cleanup). No task-environment attempt has started; the zero-attempt/zero-cleanup rollout condition remains unmet.

Rollout preparation (2026-10-08 00:31 UTC): the authority's 23 configured worker principals remain claim-disabled after the SIGHUP drain; `environment_handlers` remains empty. Read-only SQLite state is 3 running attempts (`zz-joe-e2e-3/4/5`), 1 tower cleanup, 0 queued jobs, and 0 task-environment/replica rows. No active attempt was canceled. The staged candidate release and installed release have identical `_deps` trees (204 files, 8,908,257 bytes); the candidate's `task_environments.py`, `worker.py`, and `supervision.py` hashes match the `887fe3e4` worktree. A mode-0600 runtime-drain config based on authority candidate v16 disables all 23 worker claims and sets `environment_handlers` empty; both the installed `9fddd795` and candidate `887fe3e4` config/reload parsers accept it. It is staged, not active. The updated deploy/check tools are staged separately on the authority. A release-script defect was found: its `99-zz-release.conf` sorted before the live `zzzz-release-main-9fddd795.conf`, so a restart could keep loading old code. Commit `ac9dfe67255a487190e05c2b5910d7e7b44a2af1` fixes the ordering and requires the live process `PYTHONPATH` to match before reporting deployment; `bash -n` passes and the change is pushed to Livestack `main`. ZZOPS doctor is healthy; the Benchday deploy fence is open, and current train status reports 8 waiting cargos. No task-environment workload, authority restart, handler activation, changed-assertion train, full/coalesced E2E, or publish was initiated. Keep tasks 3.2, 3.3, 3.5, 4.1, 4.4, and 4.5 open pending admitted-worker/runtime evidence.


Rootless Docker lease-expiry acceptance (2026-10-08 00:35 UTC): on zz-joe, current Livestack candidate `ac9dfe67`, isolated venv `/tmp/livestack-taskenv-rootless-20261008`, and a private HTTP/SQLite authority, `tests/test_workload_task_environment_docker.py::test_lease_expiry_cleans_rootless_container_before_releasing_task_environment` passed (1 test, 13.96 s). It observed the system-manager attempt unit/cgroup and verified container, process and socket teardown while retained environment disk remained parked with no replica advertisement or compute claim. This is candidate-path acceptance, not live admitted-worker enrollment. It does not establish a task result for any ZZOPS full/coalesced gate.

Retired tower cleanup receipt (2026-10-08 00:41 UTC): read-only authority state has exactly one `cleanup` attempt and no `running` attempts, environments or replicas. Attempt `07f3acc2cd314af2964e24da1837dd11` belongs to worker `xc-tower-e2e-1`, job `c90163626b4740e38cc4d2105f580771`; the job is terminal `failed` with reason `execution lease expired` (updated `2026-09-23T04:18:33Z`), and the attempt has no result/assertion evidence. Its journal still records phase `running` under old boot `d87ec290ef6748c180a9b70b5c616fbb`; the current tower boot is `fa670289-5f71-4081-8f75-ba64bdd480cf`, and the exact systemd attempt unit is `not-found` in both managers. The retired worker service is absent and its authority principal is claim-disabled. This is a stale failed full-E2E cleanup record, not a verdict for a current generation or for task-environment acceptance. The workspace and journal are retained until this receipt is recorded and the normal one-shot reconciliation reports cleanup to the authority.


Retired tower reconciliation complete (2026-10-08 00:48 UTC): the staged `887fe3e4` worker implementation's one-shot `reconcile()` returned successfully with `observe_only=true`; no `step()` or claim was invoked, and handler-registry sync was intentionally skipped so this retired slot did not install packages. The normal report/cleanup handshake completed, `active.json` and the attempt workspace are absent, and a follow-up authority read shows the worker re-registered under a new boot with `ready=false`, no running/cleanup attempts, and zero environments/replicas. The old attempt/job rows are now absent; the authority's terminal retention is 259,200 seconds, and its cleanup code excludes cleanup attempts from pruning until a worker's cleaned receipt ends them. The empty handler-store directory created by the one-shot constructor was removed. This confirms stale cleanup only; it supplies no test assertion result. ZZOPS still reports one dispatched Joe train and 14 waiting cargos, so the authority and worker releases remain untouched.


Live rollout checkpoint (2026-10-08 00:50 UTC): ZZOPS still reports one dispatched train on `zz-joe-e2e-4`, 14 waiting cargos and zero fresh runners; its app fence is open. The Livestack authority remains active with zero running/cleanup attempts and zero task-environment rows after the tower cleanup. `xc-tower-ubuntu` root is 97% used with 65.3 GiB free; `diskreap status` reports Low. The quick read-only scan exceeded 60 seconds at about two CPU cores and was interrupted; no cleanup was applied, and the prior 12-hour-old 113.5 MiB plan is stale. No authority restart, worker rollout, task-environment request, full/coalesced E2E or publish was started.



Safe-rollout drain outcome (2026-10-08 01:13 UTC): the global ZZOPS deploy fence holder `taskenv-authority-20261008T0102Z` was acquired for the Livestack authority rollout and drained in abort mode for 600 seconds. The drain returned `drained=false`, named five outstanding Benchday trains (`tt_082d4f94-24f9-460c-906c-dc4c67eb1a4b`, `tt_4e1cb991-6808-4bb0-a2ff-f22102d5ab54`, `tt_5195f4b2-c3b6-4e73-97c8-c880cffc7605`, `tt_8e4708b2-4b4a-40e3-8b8f-89f62a6ee51e`, `tt_f96d8a4a-7f1e-45c6-a103-0f10f5777dee`), and automatically released both Benchday and Askafox fences. A follow-up fence-status read confirmed both are open. The Livestack authority remains unchanged: six running attempts, zero task-environment/replica rows, root filesystem 97% used with 66 GiB free. No authority restart, worker activation, task-environment job, full/coalesced E2E, or publish occurred. Keep rollout and admitted acceptance open until a later drain reaches zero attempts and cleanup.



Capacity recheck (2026-10-08 01:17 UTC): `zzops train status --app benchday --config ~/.config/zzops/config.human.json` reports 8 pending/waiting cargos, 0 fresh runners, 6 dispatched trains and 5 dispatch-pending trains. The long completion train `tt_4e1cb991-6808-4bb0-a2ff-f22102d5ab54` now reports an active job on `zz-joe-e2e-1`; it is not a verdict for this task. The Livestack authority read at 01:17 still shows 6 running attempts and 0 task-environment/replica rows. No new fence, task-environment workload, full/coalesced E2E, authority/worker change or publish was initiated.



Capacity turnover recheck (2026-10-08 01:24 UTC): a read-only Livestack SQLite snapshot now has 1,176 ended attempts (three more than the 01:17 snapshot), six running attempts, zero cleanup attempts, and zero task-environment/replica rows. No task-environment work is active; the queue is turning over but has not reached the required zero-attempt window. No new fence, task workload, full/coalesced E2E, service/worker change or publish was initiated.



Active-attempt liveness readback (2026-10-08 01:33 UTC): all six authority attempts are live, not stale: worker heartbeats are 1.7–9.5 seconds old, each attempt lease has about 114 seconds remaining, and all six workers report `ready=true`. The read is based on the authority's SQLite attempts joined to worker heartbeat/readiness rows. It confirms the safe next step is to wait for completion; do not restart or drain around these active jobs. Task-environment and replica row counts remain zero.

Capacity recheck (2026-10-08 07:36 UTC): the read-only live authority still has six running attempts across `xc-win-1-wsl` and `zz-joe-e2e-1` through `zz-joe-e2e-5`; all six worker rows are ready, heartbeat ages are 1–11 seconds, lease time remaining is 110–119 seconds, and there are zero cleanup attempts, task environments, or replica rows. ZZOPS status at 07:38 shows one shared-tree cargo waiting because no fresh runner holds its fingerprint, zero stranded cargos, and a 7.19-hour wait; this global status is not this change's verdict. No rollout or new task workload is safe yet.

Parked-environment drain control (2026-10-08 07:38 UTC): `node-py/tests/test_task_environments.py::test_parked_environment_does_not_block_host_deprovision` passed locally (1 test, 0.24 s). It creates a real parked local replica, exercises the actual HostBroker lease counter with an idle fleet-view row, then drives host drain and provider teardown; the replica remains parked after release. This closes the local drain/deprovision control only.

Local receipt identity join check (2026-10-08 07:38 UTC): `node-py/tests/test_workload_environments.py::test_writer_is_exclusive_and_environment_receipt_parks_after_cleanup` passed locally (1 test, 0.33 s) after adding explicit assertions that the completed job, producing attempt/host, environment handle/generation and retained phase receipt agree. This is fixture evidence only; real admitted compilation and the end-to-end decision-ledger join remain open.

Focused local regression (2026-10-08 07:41 UTC): `node-py/.venv/bin/pytest -q tests/test_task_environments.py tests/test_workload_environments.py` passed 40/40 in 21.64 seconds. `openspec validate reuse-task-environments` passes and `git diff --check` is clean. These are local fixture checks, not admitted-worker rollout or compiler evidence.

Decision identity and placement regression (2026-10-08 07:45 UTC): the authority now generates a stable decision ULID when it creates an attempt, returns it on the assignment/compilation receipt, and exposes it in job attempt history. The migration and replay controls pass (`test_attempt_decision_id_column_migrates_additively`: 1/1; `test_writer_is_exclusive_and_environment_receipt_parks_after_cleanup`: 1/1). The focused environment/store suite (`test_task_environments.py`, `test_workload_environments.py`, `test_workload_store.py`) passes 92/92 in 11.10 seconds. External fleet decision-ledger correlation and admitted compiler measurement remain open.

Compilation-verifier identity controls (2026-10-08): the worker now checks that the authority's compilation receipt carries the same `decision_id` as its journaled assignment. `test_verified_launch_receipt_preserves_decision_id` and `test_mismatched_decision_id_refuses_before_compiler` passed 2/2 against the local systemd verifier and disposable HTTP/SQLite authority (no compiler subprocess); the mismatch refuses before handler execution. Real admitted compilation and external fleet-ledger correlation remain open.

Scheduler-to-attempt identity (2026-10-08): `placement.py` now passes the stable ID into the pure scheduler before admission, then stores that same value on the attempt. The writer/receipt integration asserts the scheduler decision and claim share the attempt-history decision ID, then joins the environment outcome by job, attempt and generation; external Fleet ledger recording remains open.

Exact-source candidate staging (2026-10-08 08:02 UTC): built the Livestack release from `b0ddce28` (content hash `84c8d0c30b8d38690e3138d219718b232199332be6f3a2e0b399e7c982a72e23`, 381 files, including the installed authority's 8,908,257-byte `_deps` bundle). `tools/check-authority-release.py` passed static, boot, worker-registration and job-round-trip stages. The candidate is staged at `/home/ubuntu/.local/share/livestack-workload-releases/livestack-b0ddce28` on `100.64.0.18` and `100.64.0.24`; both supported per-file verifications report `IDENTICAL`. No service, configuration, active release pointer or process changed. The 08:02 read-only authority snapshot has six live running attempts with fresh heartbeats and ready workers, zero cleanup attempts and zero task-environment/replica rows; ZZOPS has one shared-tree cargo waiting because no fresh runner holds its fingerprint. Keep activation and admitted acceptance open until a safe window; this is not the task's gate verdict.

Verified wait (2026-10-08 08:04 UTC): after a 30-second wait, the same six authority attempt IDs remained live: `6d9489aaed2d464e91e67358171227b0` (e2e-4), `a1d7871f0bc2455196cf337511ed862e` (e2e-2), `20690c9d26364f4e9096f8eaffe63107` (e2e-3), `cab26545877d43158cf8f03c203e1e81` (e2e-1), `194e0063143b4615b0a2f487389279c5` (release), and `c8e4231b145d4eda909f7e434641f4e7` (e2e-5). Each was still `running`, ready, with a 1.8–8.9 s heartbeat age and 110–116 s lease remaining; total cleanup stayed zero. ZZOPS still had one cargo waiting for a fresh runner. No activation or new workload is safe.

Capacity turnover (2026-10-08 08:06 UTC): the exact `zz-joe-release` attempt `194e0063143b4615b0a2f487389279c5` ended naturally. The other five observed IDs (`6d9489aaed2d464e91e67358171227b0`, `a1d7871f0bc2455196cf337511ed862e`, `20690c9d26364f4e9096f8eaffe63107`, `cab26545877d43158cf8f03c203e1e81`, `c8e4231b145d4eda909f7e434641f4e7`) remain running with fresh heartbeats and ready workers; total cleanup remains zero. ZZOPS still reports one waiting cargo and no fresh runner for its fingerprint. The task-environment candidate remains staged and inactive.

Current active set (2026-10-08 08:08 UTC): a new attempt `4b2ed480d9d944089a403d31d2bff128` is now running on `xc-win-1-wsl`; the five Joe e2e attempts above remain live. All six report ready workers and fresh heartbeats, with 110–113 seconds left on their leases; cleanup, environment and replica counts are zero. ZZOPS still has one shared-tree cargo waiting for a fresh runner. No authority/worker activation or task-environment workload is safe.

Capacity recheck (2026-10-08 07:48 UTC): the live authority reports six running attempts, all six workers ready, a maximum heartbeat age of 9.9 seconds and 114.7–117.1 seconds remaining on their leases; task-environment and replica counts are zero. ZZOPS reports three shared-tree cargos waiting because no fresh runner holds their fingerprints, zero stranded cargos, and an oldest wait of about 7.35 hours. This is fleet queue status, not a verdict for this change. Authority/worker rollout and admitted compiler acceptance remain unsafe while those attempts are active.

Fresh capacity and authority readback (2026-10-08 08:27 UTC): ZZOPS reports three shared-tree cargos waiting, zero stranded cargos, zero fresh runners, and an oldest wait of about 8.0 hours. The read-only authority SQLite snapshot has five running attempts, on `zz-joe-e2e-1` through `zz-joe-e2e-5`; each worker is ready, heartbeat ages are 1.8–9.6 seconds, and leases have about 114.8–115.1 seconds remaining. There are zero cleanup attempts and zero task-environment/replica rows. These are shared-fleet capacity observations, not a verdict for this change. No attempt was interrupted, and no task-environment workload, rollout, full/coalesced E2E, or publish was started. Keep admitted acceptance and live rollout open until the safe rollout window is available.

Current-source candidate preparation (2026-10-08 08:36 UTC): on `lappy-bellinzona`, built the worker release from `c76585e7` (231 files, content hash `6615b9ab7f29dcd0ded0a28713eba0eb8c735a1d7a2ff816faef80a3b0ec6628`). The authority candidate uses that source with `_deps` copied read-only from the active `livestack-9fddd795` release; `tools/check-authority-release.py` passed static, boot, worker registration and job round-trip under Python 3.14.7. Staged the worker bundle at `/home/ubuntu/.local/share/livestack-workload-releases/livestack-c76585e7` on `100.64.0.24`; the supported verifier reports `IDENTICAL` (same hash, 231 files). The worker units still point at `livestack-70a11344` (worker 3 uses its existing discovery overlay); no pointer, process or authority config changed. At 08:36, the live authority still had five fresh running attempts and zero cleanup/environment/replica rows. ZZOPS reported four shared-tree cargos waiting, zero stranded, zero fresh runners, five dispatched trains and three circuit-open dispatch-pending trains; the full completion train on `zz-joe-e2e-5` is existing shared work. No task-environment workload, full/coalesced E2E, or publish was started. Authority/worker activation and admitted compilation remain open until the shared work drains.

Natural capacity turnover (2026-10-08 08:40 UTC): authority attempt `8230977cfe0e4f0186e032f6e0307363` on `zz-joe-e2e-4` ended. Four attempts remain running on e2e-1/2/3/5, all with ready workers and fresh heartbeats; cleanup, task-environment and replica counts remain zero. ZZOPS still has four shared-tree cargos waiting, zero stranded cargos and zero fresh runners; four trains are dispatched and four are dispatch-pending, including the existing full completion on e2e-5. No environment job or rollout was started; the exact-source worker bundle remains staged but inactive.

Task-environment capability readback (2026-10-08 08:48 UTC): the live authority config has an empty `environment_handlers` map, and all 20 worker reports with heartbeats under 120 seconds advertise an empty `environment_profiles` map. No live worker can accept an environment-selected task yet. The authority and worker candidates remain inactive; no policy or process was changed. This readback is rollout evidence, not an E2E verdict.

Consumer handler staging and capacity recheck (2026-10-08 08:55 UTC): the active authority's handler-release CLI now lists only the selected Flutter compilation (`661ee6a9c1a0a2dcd66c44d5c2027bc4666d7e3a2a12178100c82073060bd2b5`), Rust compilation (`02e3f3ac4dce613804c3a90901f093c2e931ce859838ce5cf1a0d208d18ed708`), and task-E2E (`4effaa916f2060f7d561fddc65d0f07ad3f50c68e62a0d00dc9cea7980f5b3ce`) as newly staged records. Generation 22 and all active defaults remain unchanged; no full-suite or release package was staged. One ZZOPS full-completion attempt is live on `zz-joe-e2e-5`; authority cleanup, task-environment and replica counts are zero. ZZOPS reports 11 waiting cargos and zero stranded. `environment_handlers` remains unset, so no live task-environment handler is enabled. No task-environment job, worker/authority activation, full/coalesced E2E for this change or publish was started; rollout remains open pending safe drain and capability readback.

Authority candidate staged without activation (2026-10-08 09:02 UTC): copied the already checked authority release to `/home/ubuntu/.local/share/livestack-workload-releases/livestack-c76585e7` on `100.64.0.18`. `node-py/RELEASE.json` identifies source commit `c76585e7484a166e2b7316699923e33a937b5448`, content hash `6615b9ab7f29dcd0ded0a28713eba0eb8c735a1d7a2ff816faef80a3b0ec6628`, and 231 source files; local and remote file paths/content hashes match, and rsync reports no remaining changes. The active systemd drop-in still selects `livestack-9fddd795`; authority generation remains 22, environment handlers are disabled, and environment/replica rows are zero. The host root is 91% used with about 165 GB available. At 09:02, two completion attempts were active on `xc-win-1-wsl` and `xc-win-1-wsl-2`, with zero cleanup attempts; ZZOPS showed nine waiting cargos and zero stranded. No fence or service change was made; deployment remains pending a safe drain.

First live worker profile rollout (2026-10-08 09:12 UTC): verified the staged `livestack-c76585e7` release on zz-joe (`IDENTICAL`, 231 files, content hash `6615b9ab7f29dcd0ded0a28713eba0eb8c735a1d7a2ff816faef80a3b0ec6628`). With no active attempt on `zz-joe-e2e-2`, installed its existing mode-0600 candidate profile config (SHA-256 `a42605bbefaedd2f5ebaf16dc679ebce276600f31fdb65a119a77519deea439e`), added a new `90-taskenv-release-c76585e7.conf`, and restarted only that worker. The unit is active on the candidate release; authority reports it ready with Flutter, Rust, and task-E2E profiles and no handler activation failures. Its ext4 project-quota mount/helper is present, and the earlier positive control observed `EDQUOT`. Authority code remains `livestack-9fddd795`, handler generation 22; its `environment_handlers` map remains empty, with zero environment/replica rows, so no task-environment attempt has run. Six unrelated attempts were still active at 09:12, so the authority restart and policy enablement remain deferred. The authority config also has an independent `claim_enabled=false` for `xc-win-1-wsl-2`; I did not overwrite it. No task-specific E2E or full/coalesced train was started for this change.

Deploy-drain outcome and capacity readback (2026-10-08 09:54 UTC): the global ZZOPS fence for Benchday + Askafox entered `abort` drain and ran the full 1,800-second deadline, then returned `drained=false` with full-E2E train `tt_d025d0d9-dce2-49c4-95d9-07df64a88ecd` as the straggler; ZZOPS automatically reopened both fences. No authority or handler setting, service pointer, or process changed. At 09:54, read-only ZZOPS reports pending/waiting/stranded cargo counts of 0 and the Benchday fence open. The authority DB has seven running `benchday.e2e.full.v1` attempts, one queued `benchday.release.prepare.v1` job, zero cleanup attempts, and zero task-environment/replica rows. The authority remains on `livestack-9fddd795`, generation 22, with environment handlers empty; worker 2 still advertises the three candidate profiles. No task-environment workload, full E2E, or publish was initiated for this change. Keep authority/handler activation open until the active full-E2E and release work reaches a safe drain window. This is fleet capacity evidence, not this change's gate verdict.

Current-main task-environment rollout (2026-10-08 13:16 UTC): the staged cbeafb06 release was older than current main, so built and staged current `d37151f2568176be35ccfbaacbcf8a5bc583e95e` instead. Its 232-file content hash is `3707ceaf73cc48b58d1d2cdef967d0ded5a001935e1dbece04505a38354d5944`; the release verifier reports `IDENTICAL` on zz-joe. Focused `test_task_environments.py`, `test_workload_environments.py`, and `test_workload_docker_cache.py` passed 62 with 4 skipped locally. Worker 2 was restarted only after its own attempt/cleanup/result state was clear, its `active.json` was absent, and the host had no E2E child. Its raw authority report now carries Flutter, Rust, and task-E2E profile digests with no activation failures. The authority config already has eligible development/task-E2E handlers and forbidden full-E2E/publishing profiles; worker 2 was re-enabled by a principal-only validated SIGHUP reload, with no authority restart. Correction to the earlier 09:12 note: the live worker's profile report had gone stale before this restart; the new report is the current readback. The first Benchday Flutter environment job `2be6b72417554d5dbb2f4b4dc7e786d5` is queued behind the pre-existing coalesced completion job `913cba6b8d264ed5a040f1dec9ff531f` on worker 2, so no environment execution, reuse receipt, or timing is available yet. No full E2E or publishing was started for this change.

Live Benchday handler integration result (2026-10-08 13:40 UTC): the first admitted environment-backed Flutter attempt reached worker 2 and materialized an environment, then Benchday's installed handler returned infrastructure exit 75 because it assumed input and output shared a filesystem. Livestack's environment bind mount is intentional; no Livestack source defect was found. The stale queued request was cancelled after diagnosis. Benchday updated both bounded-workspace guards and has 5/5 focused local tests passing; handler bundle rollout and fresh admitted acceptance are still open. No task-specific E2E or publishing was started.

Benchday handler compatibility acceptance (2026-10-08 13:48 UTC): the filesystem refusal was fixed and landed in Benchday commit `3097423db233cccc75ab3d8439d7c620f60f8453`. Its immutable Flutter handler release `172a0e34ddc8c5e3fe9aaa599c9bab1edd1b29331ab50d8acf3e7b049a824b20` is active at authority generation 28, and worker 2 reports the release installed with no activation failures. Fresh environment-backed Flutter job `74d5f69694744cbf8a50e12580d7051c` is queued behind an unrelated coalesced E2E attempt; no second environment attempt has started yet. Livestack source remains unchanged. Keep compiler, reuse, task-specific E2E and savings acceptance open until the job finishes.

Post-restart admitted reuse check (2026-10-08 16:10 UTC): after the worker was running Livestack `d37151f2` with Flutter/Rust/task-E2E profile readback, five same-source Flutter jobs (`379e34049b7f421dabf19aa9a96dc7c4`, `a3dea805792d4c2393ddc3513995e49c`, `13c72d91d3ef4e10b98ba6fac2b755df`, `8ec214b5d9034cf490859c1348d4dc39`, `822e3da866c542b98bec410bd3dd9921`) succeeded on `zz-joe-e2e-2`, all with the same source digest and environment handle, but every receipt said `reuse_outcome=rebuilt`, `reason_code=authority_replica_unconfirmed`, and cache outcome `created`. The fifth assignment included the prior parked generation-9 replica, yet generation 10 still rebuilt. The current authority row and host marker are parked at generation 10; attempts ended and no compute remains held. Five compile phases total 167.414 seconds, so this run set shows no avoided compile work. The receipt does not identify which local reuse predicate failed; Livestack source has not been changed on this evidence alone.

The admitted no-environment Rust `check cli` job `3ac0801f10204961adc61d91ecee9a7c` succeeded on `zz-joe-e2e-1` with exit 0 and 45.596 seconds of Cargo check time; it verifies the installed Rust handler only, not retained Cargo state. The environment-enabled Rust job `dcfa9dad52cf48668b77e784005f6a9d` remains queued because the only worker with the Rust profile is occupied. The explicit task-E2E environment request was refused before capture with `environment_unsupported: task E2E handler is not enrolled`; no task-E2E job was created. No full/coalesced E2E or publishing was run for these checks.

Task-E2E worker contract diagnosis (2026-10-08 16:50 UTC): after handler enrollment, task-specific job `15e514b29c964a789de5c1b9127748d8` reached `zz-joe-e2e-2` with environment handle `5a909a3b3b6a488fa4ebcbe451dac81a`, but failed before running the selected assertion. Its command log says `unsupported task E2E execution request`; the handler checks the accepted input digest against `HARMONY_INPUT_DIGEST`, which Livestack only added for compilation assignments. The worker then reported a missing captured-source link while parking the environment; the early handler refusal preceded Benchday's source-link restoration. The shared worker now exports the digest for every workload, with a regression assertion; all 5 tests in `node-py/tests/test_worker_lease_env.py` pass. Worker release rollout and a fresh task-specific E2E run remain open. No full/coalesced E2E or publishing was run.

Receipt-compatibility correction (2026-10-08 22:08 UTC): Livestack commit `8ba0e0c6` initially added detailed replica mismatch reason codes to worker receipts, but the live authority still validates the previous closed version-1 reason list. This follow-up restores that list, maps newer worker-only diagnostics to the accepted `authority_replica_unconfirmed` receipt code, and records the exact failed reuse/replica predicates in a length-bounded worker warning. The regression verifies all six replica mismatch cases, checks the specific warning diagnostic, and validates the emitted receipt against the existing worker schema. `node-py/.venv/bin/pytest -q tests/test_task_environments.py` passed 28/28; `openspec validate reuse-task-environments --type change` and `git diff --check` pass. No worker package was installed and no live reuse/savings claim is made; worker rollout and a fresh admitted repeat remain open.


Current rollout and train recheck (2026-10-09 05:09 UTC): the durable change-specific admission cargo `tt_743d3c2c-a167-40ba-8c0b-6679063c7543` was reattached with `zzops train watch` after the coordinator restarts and returned terminal PASS for the four derived task-environment assertions (`partial: true`), on producing commit `43681dcedea56cf1d96d4556d48d90bacb2e76ed`. The Livestack authority candidate at `d55c4c13` passes its throwaway release check and is staged/verified identical, but the global abort-mode drain still has two active attempts; no service pointer/config/process changed. See [rollout recheck evidence](evidence/rollout-recheck-20261009.md). No task-specific E2E, full/coalesced E2E, or publish was started.

Drain deadline update (2026-10-09 05:16 UTC): the global abort-mode drain returned `drained:false` after 600008 ms on full-suite train `tt_33ef694c-76a2-468f-aa67-52b2fe4bffcb`; ZZOPS automatically released Benchday and Askafox holds. The authority independently reports one running full-suite attempt on `zz-joe-e2e-1` and zero cleanup attempts. It remains untouched; authority deployment and admitted reuse acceptance are still pending a new safe idle window. See [rollout recheck evidence](evidence/rollout-recheck-20261009.md).

Admitted same-source repeat (2026-10-09 05:34 UTC): Benchday jobs `3dd5547e84e2462985360a8d31d8886a` and `8aa13fe7bbab4a038532136460a019cd` both succeeded on `zz-joe-e2e-2` with identical Rust source digest, but both receipts were `rebuilt` / `authority_replica_unconfirmed`; generation advanced 5→6 and compilation took 28.09 s then 28.73 s. The authority has a parked generation-6 replica and zero cleanup attempts. This is a concrete no-reuse reproduction against the pre-fix authority; d55 rollout and post-fix admitted acceptance remain open behind the running full-suite train. Detailed phase timings are in the Benchday change evidence.

ZZOPS restart and authority recheck (2026-10-09 05:53 UTC): the selected four-assertion admission for landed merge `48c6d99f32a5ec93a87b6375953bec2fa93fed43` passed; its completion cargo `tt_bf57dfab-ec13-439b-9883-37771fac8048` remains active under watch on `xc-win-1-wsl-2`. The unrelated full-suite attempt and two release attempts remain active on the authority, with zero cleanup jobs, so the authority candidate `d55c4c13` was not installed. A fresh global fence could not be checked/acquired: the canonical `zzops-admin fence-status` failed with `EACCES` spawning `esbuild`. No authority or worker process/config was changed. See [rollout recheck evidence](evidence/rollout-recheck-20261009.md).

Changed-assertion gate completion (2026-10-09 05:57 UTC): completion cargo `tt_bf57dfab-ec13-439b-9883-37771fac8048` passed all four changed task-environment assertions; `zzops train status --change 48c6d99f32a5ec93a87b6375953bec2fa93fed43` reports `complete=true`, `full=false`, verdict PASS. At the 05:56 authority read, only the unrelated full-suite attempt was still running and zero cleanup jobs remained; its heartbeat was fresh, so it was left intact. Authority rollout is still blocked by the full-suite attempt and the host-side fence CLI `EACCES` failure. Post-rollout reuse/timing validation remains open. See [rollout recheck evidence](evidence/rollout-recheck-20261009.md).

Post-restart recheck (2026-10-09 06:07 UTC): reattached the change-specific completion watcher after the owner-authorized ZZOPS coordinator restarts; cargo `tt_bf57dfab-ec13-439b-9883-37771fac8048` still returns PASS for all four derived assertions. The live authority remains on `livestack-46a4bd3e`, registry generation 54; its current config maps the Flutter, Rust and task-E2E handlers to their intended profiles and full-E2E/publishing handlers to forbidden profiles. Worker 2 reports all three task profiles and worker 5 reports Flutter/Rust; 22 of 27 workers are ready. This readback supersedes the earlier note that environment policy was disabled. The sole running authority attempt is the unrelated full-suite job on `zz-joe-e2e-1` (fresh heartbeat, 118 seconds on lease); there are no cleanup jobs. The ZZOPS deploy fence is open and candidate `d55c4c13` remains staged. No attempt was canceled and no authority/worker config, pointer or process changed. Post-fix admitted reuse, invalidation and performance acceptance remain open; see the detailed [rollout recheck](evidence/rollout-recheck-20261009.md).

Authority turnover (2026-10-09 06:18 UTC): the full-suite train ended in infrastructure failure, not a test verdict. Before its slot cleared, ZZOPS dispatched an unrelated one-assertion admission on `xc-win-1-wsl-2` and two release jobs on the Joe release workers; all three attempts were actively leased, so no zero-attempt drain or authority restart was safe. The selected four-assertion task-environment change gate remains PASS. The `zzops-admin` fence-status invocation still fails with bundled `esbuild` `EACCES` after coordinator restarts; the deploy fence is open and the staged `d55c4c13` authority candidate is unchanged. No full-suite retry, cancellation, task-specific E2E, publish, or authority/worker mutation was initiated. Post-fix admitted acceptance and archives remain open.

Turnover update (2026-10-09 06:22 UTC): the Herdr single-assertion attempt returned an infrastructure outcome and ZZOPS queued a completion retry; one cargo is waiting. The live authority now has one unrelated Rust compilation attempt and two release attempts, each with a fresh lease. The full-suite train is terminal infrastructure failure, while the task-environment changed-assertion cargo remains PASS. No cancellation, fence hold, task-environment job, publish, or authority/worker mutation occurred. The latest [rollout evidence](evidence/rollout-recheck-20261009.md) records the current attempt IDs and fence-admin error.

Retry dispatch (2026-10-09 06:24 UTC): the Herdr one-assertion retry is active again on `xc-win-1-wsl-2`; one Rust compilation and one Android release are concurrently active on the Joe workers. All three are live, so the zero-attempt drain condition still does not hold. This task remains on the same staged authority candidate and passing four-assertion change gate; no task-specific E2E or publish was started.

Current rollout recheck (2026-10-09 11:52 UTC): the documented read-only fence command succeeds on its configured host, `zz-tower2` (`100.64.0.12`), from `/`; the earlier `xc-tower-ubuntu` (`100.64.0.18`) result was from a managed client and is superseded. The current fence is open, and the logged `taskenv-authority-rollout-20261009-v1` hold was released after its abort-mode drain deadline. The four-assertion Benchday change gate is PASS/complete/full=false. Read-only Livestack status shows the existing full train on `zz-joe-e2e-1` plus a Rust compilation on `zz-joe-e2e-2`, so no zero-attempt drain is available. The live observe rollout still waits on `canary_not_representative:benchday.e2e.task.v1`; Rust generation 6 and task-E2E generation 32 are parked but both last report `rebuilt`. See [rollout evidence](evidence/rollout-recheck-20261009.md). Keep rollout, reuse proof, timing acceptance and archive open; no config, worker, or authority mutation was made.

## Current capability and rollout state — 2026-10-09 12:18 UTC

This readback supersedes the earlier task 4.4 description that the live authority had environment policy disabled. Authenticated capabilities now expose environment API v1 for Flutter compilation, Rust compilation, and `benchday.e2e.task.v1`; dependency preparation, full E2E, commerce, and release handlers are forbidden. The observe rollout is generation 1586, spec generation 2, unit `unit-f38a7baa`, waiting on `canary_not_representative:benchday.e2e.task.v1`. Only `zz-joe-e2e-2` advertises task-E2E release `909844c7b71167b5dfa7ca5193d37cf41184cd28`; that worker is busy. Other Joe E2E workers have active test-train or compilation attempts, or are draining, so the authority cannot enter a zero-attempt drain. Capability and observe-mode readback do not complete installed-worker cleanup, admitted reuse, timing, or rollout acceptance. See [the current rollout evidence](evidence/rollout-recheck-20261009.md).

The 12:29 UTC generation check after the separate full-product train's infrastructure failure reports generation 1610, still `observe`, with `applied: []`. No authority rollback is due because this change did not deploy a generation. Worker 2 remains the sole task-E2E advertiser; active test-train/compiler work and worker 5's drain still block representative canary and zero-attempt rollout acceptance. See the [latest rollout evidence](evidence/rollout-recheck-20261009.md).

## Accepted task-E2E canary and rollout recheck — 2026-10-09 12:53 UTC

Benchday captured commit `d09a6f91acd53b5df6f16b724402597971812eba` with source digest `e4ef8efe3bf24773efbd55543536049878bcfcbe15981f84cce96413398104c5`; the exact four changed task-environment checks were accepted as job `a42bf0f9a88c4c00bc44cccec1c4bbca`, environment handle `06318b61f53c4b3ca5cb7dc620b5702f`, under the installed task handler release. At the 12:53 UTC read it was queued solely on `worker_busy` for `zz-joe-e2e-2`, the only task-E2E handler advertiser. It had no attempts, no retained bytes, and environment state `empty` generation 0. The worker was running a separate test-train admission with seven named checks; this is not the full/coalesced suite. Do not resubmit the accepted job.

The latest read-only rollout report (about 12:47 UTC) showed `observe`, generation 1646, spec generation 2, `applied: []`, waiting on `canary_not_representative:benchday.e2e.task.v1`; no authority or worker change from this task occurred. The ZZOPS admission phase for merge `48c6d99f32a5ec93a87b6375953bec2fa93fed43` passed the four derived assertions on commit `9abaf9af69326bc8ce816df25bd2ebc42b540411`, but completion cargo `tt_8355eb73-4f7e-4194-a5b6-b26e99f0c717` is still queued. No complete change-bound verdict, admitted environment reuse result, or measured savings is established. Full/coalesced E2E and publishing remain on their existing unified paths.

## First execution cleanup and same-handle retry — 2026-10-09 13:01 UTC

The accepted task-E2E job `a42bf0f9a88c4c00bc44cccec1c4bbca` reached `zz-joe-e2e-2`, then failed before any selected assertion because its captured Benchday source `d09a6f91acd53b5df6f16b724402597971812eba` marked two still-defined `test-train.*` checks as retired. Current Benchday source already includes landed correction `ebf93cc4d`; no change to Livestack is indicated by this product failure.

Livestack parked the failed attempt's environment at generation 1 with 419,856,384 bytes retained and no active attempt. The receipt reports `reuse_outcome=created`, 16 cache components created, queue 564.651 s, source materialization 22.648 s, transfer 6.569 s, execution 12.449 s, and cleanup 0.010 s. Compile, dependency, and test phases are explicitly unknown because the handler did not instrument them. The separate Docker cache reported a hit; that is not evidence of task-environment reuse or savings.

Benchday then captured corrected commit `07d16dfdbeec3394d468eff3307b9d2ef44cacf8`, source digest `cee8f00fa37b8bd8ec85a233510aba4550d56e74afc9d32050bd56b4866946d4`, and submitted new job `d849704d744746a584086bc0fc4b10b5` using the same environment handle `06318b61f53c4b3ca5cb7dc620b5702f` and same exact four-check selection. At 13:01 UTC it was queued solely because the task-E2E worker was busy; it had zero attempts and the existing environment remained parked at 419,856,384 bytes. This is request continuity and cleanup evidence, not a reuse result. Full/coalesced E2E and publishing remain on their existing unified paths.

## Corrected-source retry result — 2026-10-09 13:04 UTC

Task-E2E job `d849704d744746a584086bc0fc4b10b5` ran and was rejected before any assertion. The result names `test-train.gate-scoped-read-agrees` and `test-train.newer-failure-never-falls-back` as retired, but captured Benchday source commit `07d16dfdbeec3394d468eff3307b9d2ef44cacf8` no longer contains either retirement entry. The job used installed handler release digest `f786547e41962eb20782253d94adc305c6d93cd2a9b35b71506eb0b14937f8bd`; worker `zz-joe-e2e-2` advertises task-handler source label `909844c7b71167b5dfa7ca5193d37cf41184cd28`. The failure is a stale immutable Benchday handler package, with no assertion result.

The attempt cleaned up and parked generation 2 at 419,893,248 bytes, but reports `reuse_outcome=rebuilt` / `authority_replica_unconfirmed`; all 16 task-E2E cache components were created again. Queue was 217.707 s, source materialization 22.519 s, transfer 8.599 s, execution 9.733 s, cleanup 0.009 s. Compile/dependency/test timings are unknown (`handler_uninstrumented`). This does not establish a reuse saving. A corrected task-only handler candidate was built from the captured source commit; package digest `31918967766f72c71ddf035a60956a9ebb3d020c974ac769592cec7bfa06412e`, archive digest `c6ead4e583fcdfb9f055f09bc10ad011027f29b0cab4677c8404186f6fa6369f`. It is not staged or activated because this client lacks an admin principal.

Live rollout status is generation 1683, mode `observe`, `applied: []`, waiting on `canary_not_representative:benchday.e2e.task.v1`; only `zz-joe-e2e-2` advertises the task handler. No authority or worker configuration was changed. The reattached ZZOPS completion cargo `tt_8355eb73-4f7e-4194-a5b6-b26e99f0c717` and fresh change-bound status now show PASS, complete=true, full=false, for exactly the four changed assertions. This is separate from task-environment reuse evidence. Full/coalesced E2E and publishing remain on their unified paths.

## Prepared current release candidates — 2026-10-09 13:15 UTC

Built a fresh Benchday task-E2E-only bundle from current source commit `9a327575f94e374dc80571348c622d2690dedfa9`; its release digest is `217f90a89ff4878903da72a38fccc81e6824acc1f2a555ff35f290c1a18f0446` and archive digest is `c6ead4e583fcdfb9f055f09bc10ad011027f29b0cab4677c8404186f6fa6369f`. The archive hash matches and the bundle excludes both stale retired IDs. A current worker release was built from Livestack commit `074536c73b3472467056284941b18aebc399ca71`; its 245-file content hash `82bf8c6c8ff36c5d0618af314eb7fd68c815d27691157ca787c75cfbfd31893c` was independently rechecked with the release builder. Candidates remain in `/tmp`; no release was staged or installed.

At 13:14 UTC the task-E2E worker `zz-joe-e2e-2` was idle, but it is still the only worker advertising the task handler; the other four are missing it and its deployment-unit readback is `unknown`. The rollout remains `observe`, with no applied actions and no representative canary. `handler_release_cli status` refuses the only available client token because it is not an admin principal; the operator credential is absent. No service, worker, or authority configuration was changed.

## Rust cache-path follow-up — 2026-10-09

The admitted source-refresh job `bbbe018da9c2400c9a1774d39028feeb` succeeded
with both Cargo cache components reported as reused, but its log compiled a
broad dependency graph after a source change with no Rust source edits. The
worker previously mounted the stable retained source at an attempt-specific
absolute path; this is the leading explanation, not yet a compiler-isolated
proof. The worker now uses the provisioned host-stable `task-environment-view`
as its bind destination, shared across worker identities on one physical host
while each attempt remains in an isolated systemd mount namespace. Focused
Livestack checks passed 30/30 on `lappy-bellinzona` (Python 3.13.15):
`test_task_environments.py` and the existing two-invocation worker integration.
This does not prove a real Cargo fingerprint hit or cross-worker compiler
reuse. The change landed on Livestack main as `377bb4e4`, and its 246-file
worker candidate has content hash
`7cf90d52f10d4e68e9442aa493c8f8f67d0ec2a6c66526893017f9dd0d05d2c2`. It has
not been deployed or exercised by an admitted compiler. The live zz-joe mount
exists but the required sibling `task-environment-view` is absent, so do not
activate this release until that path is provisioned under the normal
fence/drain procedure. Keep tasks 3.2, 4.1, 4.4 and 4.5 open; make no savings
claim. See
`evidence/cargo-cache-path-instability-20261009.md`.

## Changed-assertion policy correction — 2026-10-09

The gate-projection notes above do not reopen accepted assertion work. Benchday
`docs/e2e-gate.md` now states that the first complete PASS for a merge's changed
assertions is final; a later infrastructure outcome cannot revoke it. Both
merge `48c6d99f32a5ec93a87b6375953bec2fa93fed43` (four assertions, train
`tt_8e65b4a4-a9f6-4d05-97a6-447f2fedacb1`) and merge
`976f344600eea42213ff91d4f2fab317e9af4965` (one assertion, train
`tt_aaff49f4-15b2-4595-98a3-5ee72977aebd`) have retained complete PASS
evidence. Later infrastructure-only attempts and a current `status --change`
`missing` response are a status-projection inconsistency; they do not require
another gate submission. Task 4.5 remains open for its independent alternating
benchmark, live rollout, rollback, and archive requirements.

## Stable worker-view prerequisite rechecked — 2026-10-09 22:57 UTC

The `zz-joe` sibling `task-environment-view` is now present, empty, and owned
`ubuntu:ubuntu` mode `0700`; both worker user units are active, with no restart
for the directory repair. On the authenticated roster read, e2e workers 1 and
2 advertise Flutter, Rust-check, and task-E2E handlers; workers 3 and 4 do not
advertise task-E2E, and worker 5 remains claim-disabled. Workers 1, 3, and 4
were executing admitted assertion work while worker 2 was running a coalesced
full-suite completion. No task-specific E2E or compiler job was submitted
against the repaired view. This is only the stable-path host prerequisite;
the admitted freshness/compile and rollout acceptance tasks remain open. See
`evidence/cargo-cache-path-instability-20261009.md`.

After the coordinator restarts, the owner explicitly requested reattachment by
resubmitting the two landed assertion-changing merges. Their current admission
cargos are `tt_142c3470-16aa-465b-b02b-cb0d74d49c98` (four assertions for
`48c6d99f32a5ec93a87b6375953bec2fa93fed43`) and
`tt_5d6014c2-b87f-4c40-9e41-27d85f5a1f01` (one assertion for
`976f344600eea42213ff91d4f2fab317e9af4965`); both were queued on snapshot
`0d62a596dbf1` at submission. Await their terminal results; this submission
does not request or represent a full-suite run.

## Gate watcher update — 2026-10-09 23:05 UTC

The reattached admission train for `48c6d99f32a5ec93a87b6375953bec2fa93fed43`
passed all four derived assertions (`tt_4ea73eed-f3f1-4cbb-80e9-b64f95978cfd`);
the `976f344600eea42213ff91d4f2fab317e9af4965` admission passed its one
runtime-freshness assertion (`tt_5a31a072-5d24-4aed-bb02-e5883dcf7ab6`). These
are admission results only. The four-assertion completion cargo
`tt_afaa9538-8a9f-4973-a109-7f878cb4fe7d` had an infrastructure-only attempt
and is queued for retry. The runtime-freshness completion cargo
`tt_90f681c9-38af-4e4f-91f2-6b2c9524ba69` is dispatched on `zz-joe-e2e-4`.
Completion watchers are attached; do not count either merge as newly complete
until its change-bound completion result is observed. The pre-existing full
train continues separately on `zz-joe-e2e-2`.

## Parked state readback — 2026-10-09 23:01 UTC

The read-only authority inspection reports the existing Rust handle at
generation 14 with 18,310,598,656 retained bytes and the task-E2E handle at
generation 9 with 2,294,231,040 retained bytes. Both are `parked`; their latest
job records are `succeeded` and their attempts `ended`. The Rust and task-E2E
last-use timestamps are 20:56 and 19:45 UTC, before the 22:53 stable-view
repair. They confirm retained state and parking, but do not close the required
post-repair compiler/E2E, invalidation, or timing evidence. Details:
`evidence/cargo-cache-path-instability-20261009.md`.

## Scoped task-E2E request queued — 2026-10-09 23:08 UTC

Benchday job `9e08c6cc02fa4c00b1dbddaffed763dc` was accepted for the exact
`fleet-workload.task-environment-runtime-freshness` check (1 of 923 known
checks), using existing task-E2E handle
`06318b61f53c4b3ca5cb7dc620b5702f`. The authority reports `queued`, no
attempts, and `worker_busy` on workers 1 and 2. The environment remains parked
at generation 9 with 2,294,231,040 retained bytes. This is not a full-suite
request and is not yet post-repair execution evidence; observe the same job
after capacity clears.

## Reattached gates and current rollout readback — 2026-10-09 23:21 UTC

The reattached completion cargo for Benchday merge
`48c6d99f32a5ec93a87b6375953bec2fa93fed43` (`tt_afaa9538-8a9f-4973-a109-7f878cb4fe7d`)
is terminal PASS for all four derived assertions, with `complete=true` and
`full=false`; producing train `tt_3dfc7a85-b40c-4b55-8ec8-52f69ba47373`
completed at `dd8311da558602478196944b2517e2447d56e5e7`. The independent
`976f344600eea42213ff91d4f2fab317e9af4965` merge has a current change-bound
PASS for `fleet-workload.task-environment-runtime-freshness`
(`tt_b2ccb762-6e29-4f15-aaf3-c98af6c64a40`), also `complete=true`, `full=false`.
These close only the named changed-assertion gates.

The authenticated rollout readback remains mode `observe` at spec generation 2
with no applied actions. The desired `unit-f38a7baa` reports `zz-joe-e2e-1`
behind and workers 2–5 unknown; task-E2E is advertised only by workers 1 and 2,
with handler-release skew between them. At the 23:21 UTC roster read, workers 1
and 2 were running full-handler attempts, worker 3 a Rust job, worker 4 another
full-handler attempt, and worker 5 was claim-disabled. The existing exact
task-E2E canary job `9e08c6cc02fa4c00b1dbddaffed763dc` remains queued with no
attempt and its environment parked at generation 9. No duplicate was submitted.

## Rollout observation follow-up — 2026-10-09 23:24 UTC

The fresh observe report (generation 2903, spec generation 2) has no set
waiting reason: the two-worker `min_claiming` threshold is currently met, so
the earlier `canary_not_representative` observation is superseded. The exact
canary is still queued solely because workers 1 and 2 hold active full-handler
attempts. The report remains `mode=observe`, `applied=[]`; worker 1 reports
unit release hash `7cf90d52f10d4e68e9442aa493c8f8f67d0ec2a6c66526893017f9dd0d05d2c2`
against desired unit `unit-f38a7baa`'s release hash
`6593d104da0263001da63df76a4f7338818053f02e8026de3ccb8b744645d20b`, and
worker 2 has no unit report. This is observed rollout drift; no worker was
restarted or changed. Keep defaults disabled pending the canary, benchmark,
and remaining rollout/rollback evidence.

ZZOPS still reports the long full-suite train
`tt_76035e95-a06d-464b-9666-a4f159b4d18d` as running on job
`473610d2f7ce4aa0b36893be668e8d32` / attempt
`575f73e842a74910b477a6d843dc8831` on worker 2; progress is unreported and
there are no shared-tree riders. It has not reached terminal failure, so it
was not cancelled. The representative canary, source/cache invalidation and
benchmark evidence, rollout/rollback acceptance, and archive remain open.

## Task-E2E dispatched after slot cleanup — 2026-10-09 23:42 UTC

The accepted Benchday request `9e08c6cc02fa4c00b1dbddaffed763dc` is now
running on `zz-joe-e2e-1`, still bound to handle
`06318b61f53c4b3ca5cb7dc620b5702f`, at environment generation 10. Direct
authority reads report `result=null` and environment state `preparing`; this
is not terminal execution evidence. The original 1800-second observer exited
with `pending`; continue reading the accepted job ID without resubmitting it.
See `evidence/task-e2e-resumed-20261009.md`.

The scheduler-owned full completion train on worker 2 was cancelled through the
audited ZZOPS command after 78 minutes without reported progress and with no
shared-tree riders. Its authority job is cancelled but cleanup remains pending,
so worker 2 is not-ready until it reports clean. The independent worker-1
full-product train ended with a ZZOPS result-selection mismatch and no verdict
for its named assertion. The only local Livestack credential is non-admin;
handler-release status refused it and no operator config is present. No handler
activation or rollback occurred. Benchmark and live rollout/rollback
acceptance remain open.

## Task-E2E terminal follow-up — 2026-10-09 23:53 UTC

The earlier queued/running observations for Benchday job
`9e08c6cc02fa4c00b1dbddaffed763dc` are superseded by its terminal PASS. The
exact runtime-freshness assertion passed on `zz-joe-e2e-1`, reused all 16 cache
components on the same task-environment handle, and parked generation 10
after clean teardown. Its 1,667.505-second queue is separate from compile and
test phases, so this is environment-reuse evidence, not a queue-savings claim.
See `evidence/task-e2e-resumed-20261009.md` for artifact digests and phase
receipts.

This closes only the post-repair task-E2E execution. The alternating compiler
benchmark, real Dart/native and deletion/symlink/lockfile invalidation
controls, broad worker/SDK rollout, rollback readback, and archive prerequisites
remain open. Current rollout status is `mode=observe`, generation 2970,
`applied=[]`; the unit report lists `zz-joe-e2e-1` behind and workers 2–5
unknown. The authority roster lists the task-E2E handler on workers 1 and 2,
both occupied by background image-warm jobs in the snapshot; workers 3 and 4
were idle without that handler and worker 5 was claim-disabled. Keep
automatic-selection defaults disabled.

## Flutter development handle terminal follow-up — 2026-10-10

Benchday's accepted Flutter job
`198fd8b68bfc4b418e2f1dd681a9a1e6` succeeded on `zz-joe-e2e-2` using the same
parked environment handle at generation 11. The environment was parked again
after cleanup. The `flutter-native` cache component was invalidated, so this
confirms reattachment and resource release but not a warm cache hit or saved
compile time. Queue remained 100.336 seconds and host CPU pressure was elevated.
The same-source repeat `a588d240751b4cf3a9dcb59623b301cf` reused the
`flutter-native` component and measured 29.516 s compile versus 37.905 s on the
prior attempt; its queue was 1.245 s versus 100.336 s while host load was also
lower. This is an observation, not a controlled savings estimate. The caller
receipt does not close the Dart/native alternating benchmark, source/cache
invalidation controls, worker rollout, rollback readback, or archive. See
`evidence/flutter-stable-handle-reattachment-20261009.md`.

## Alternating benchmark follow-up — 2026-10-10

Benchday's actual wrappers completed an invalidated/repeat/Dart-edit Flutter
sequence and a Rust daemon source-edit control on `zz-joe`; queue, preparation,
test, compile, CPU-core-second and host-load evidence is in
`evidence/alternating-benchmark-20261010.md`. The Rust edit log checked only
`benchday-daemon`, with no dependency crates recompiled. An earlier real daemon
code change is the timing-instrument positive control. This supersedes the
prior note that no Dart-edit run existed. Livestack task 3.2 remains open for
deleted-file, symlink, lockfile, compiler/ABI, and compatibility invalidation
controls. Worker rollout, rollback, and archive work also remain open.
Full/coalesced E2E and publishing were not run.

## Source-deletion control — 2026-10-10

The admitted Rust mirror reconciliation control succeeded: a file-present job
compiled a temporary module, then a second captured source deleted the file
while retaining its module declaration. It failed with the expected
`E0583 file not found` rather than using stale mirrored source; both attempts
ended parked with Cargo caches reused. See
`evidence/alternating-benchmark-20261010.md`. The internal symlink wrapper
probe failed twice before admission with `Broken pipe`, so it supplies no
symlink verdict. Lockfile and toolchain/ABI invalidation remain open; task 3.2
is not complete.

## Rollout readback after authority restart — 2026-10-10

The fresh report is generation 3051, mode `observe`, `applied=[]`, spec
generation 2. Worker 1 is behind, worker 2 unknown/stale, and workers 3–5
unknown; proposed actions are `observed_only`. The rollout fence expired at
11:22 UTC and no operator config is present in the workload config directory.
No rollout or rollback was attempted. Keep automatic selection disabled and
task 4.4 open pending an operator-authorized rollout/readback.

## ZZOPS restart recheck — 2026-10-10

After the owner-reported coordinator restarts, change-bound status for
`be1d22ac4787de55a17ca31247ef6edb5baad12b` returned `superseded`: its five
assertion definitions were amended in snapshot
`1d4ff5835ff1c8632902fe410bf3b04ff97397a0`. The status names producing train
`tt_50ecbfd9-4129-4e9c-a4b4-5831f25e5fd9`, commit
`91645743d3335e5cda736390c30af5a261799094`, state `done`, verdict `pass`.
Re-running submit for `be1d22ac...` returned `superseded` before admission and
created no cargo. Per-change status for `1d4ff583...` and `91645743...` is
`not-applicable` because neither merge changed an isolated-E2E assertion
definition. The global `status.gate` field was not used as this run's verdict.
A watch using the train ID returned 404 because watch takes a cargo ID; no new
cargo was created, so there was no new watcher to attach. No full-suite request
or publishing was run.

## Source-upload capacity recheck — 2026-10-10

The retry-enabled Benchday caller and the scheduled source-tick uploader both
failed three source-object PUT attempts with EPIPE before job submission. No
new admitted attempt or compiler output resulted; the existing task environment
handle remained parked at generation 18. The caller-side source-publisher
regression suite passes 21/21, but retries do not resolve this live failure.

Read-only authority inspection found a configured object-store maximum of
225,485,783,040 bytes (210 GiB), an object directory reported as 210.0G, and
about 410 GB free on the authority host filesystem. The non-admin caller cannot
read the blob status endpoint, so max-capacity rejection is a strong inference,
not a confirmed endpoint verdict. No stored objects or shared authority
configuration were changed. Keep source reconciliation (3.2), rollout/readback
(4.4), and companion acceptance/archive (4.5) open until admitted source and
rollout evidence can be collected.

The transport failure's code path is now confirmed: upload-grant issuance did
not check the logical object-store cap, while BlobStore rejected a full store
before consuming the PUT body. This surfaced to large-body clients as EPIPE and
left a grant appearing issued. The worktree now preflights capacity and records
a bounded refused_capacity event before issuing the capability; the focused
upload-grant suite passes 15/15. This fix is not landed or deployed, does not
free live storage, and is not rollout or admitted compiler evidence.

## Authority candidate and fenced-drain recheck — 2026-10-10

Commit 486cf6e5 is on Livestack main. Candidate release livestack-486cf6e5
contains matching hashes for the changed BlobStore and upload-grant files; the
authority release checker passed static, boot, worker-registration and job
round-trip stages on both the local scratch candidate and target host.

The ZZOPS deploy fence taskenv-authority-rollout-20261010-v1 was acquired, but
its 600-second abort-mode drain timed out and released the hold. It named two
in-flight graph nodes and no train, release plan or publish. A read-only run
status showed the associated graph still progressing (six nodes succeeded,
two running). The authority was not restarted, no queued job was cancelled,
and no live storage or task-environment configuration changed. Keep rollout,
live capability/cleanup readback and admitted compiler acceptance open until
the graph reaches terminal state and a fresh bounded drain succeeds.

## Live pointer recheck — 2026-10-10 01:55 UTC

Read-only systemd state on authority `100.64.0.18` reports
`livestack-workload-authority` active since 2026-10-09 10:40:21 EDT. Its
current release drop-in still selects `livestack-d55c4c13`; the candidate
`livestack-486cf6e5` is staged but not active. The candidate's `blobs.py` and
`upload_grants.py` hashes match the Livestack worktree. The authority root has
407 GiB free (77% used). ZZOPS still reports two dispatched trains without
worker progress samples, so no new drain or authority restart was attempted.

## Bounded CAS capacity candidate — 2026-10-10 01:58 UTC

The admin-only status and dry-run retention plan show 224,329,065,958 bytes in
3,156 objects against the 225,485,783,040-byte cap, with 436,498,710,528 bytes
free on the filesystem. There are no expired references or unreferenced blobs
eligible for collection. A private candidate config is staged at
`/home/ubuntu/.local/state/livestack-workloads/authority-taskenv-20261010.candidate.json`;
it raises the bounded object cap to 250 GiB while retaining the 50% filesystem
cap and adding the owner-approved 10% headroom floor (40 GiB minimum). The
active release schema accepted it; mode is 0600 and SHA-256 is
`6baf20be04d1d21775efb3320b057f42d78bf3a6d0f81c1e80f7367b82ee30d8`. It is
not installed. Await terminal ZZOPS trains and a fresh successful fenced drain
before deploying the staged `486cf6e5` authority release with this config.

## Development-profile rollout on workers 3 and 4 — 2026-10-10

Workers `zz-joe-e2e-3` and `-4` were updated from the current Livestack main
release `1cfdc69b` with only Flutter and Rust development profiles, then
re-enabled after idle restarts. The authority stayed untouched; neither worker
received a task-E2E profile. Worker 3 received an existing ZZOPS full-suite
admission. The queued Benchday Flutter probe is now running on worker 4 with
the same saved environment used by workers 1 and 2. It reports reuse at
generation 24; its terminal receipt remains pending. Details and backup paths
are in `evidence/worker3-dev-profile-rollout-20261010.md`.

### Worker 4 terminal follow-up

The pending status above is superseded: Flutter job
`3520e1a97e4b45789cea2f05890bc3cd` succeeded on worker 4 using the same
saved environment handle previously used on workers 1 and 2. It returned
`reuse_outcome=reused`, `source_updated_incrementally`, generation 24, and a
parked receipt after cleanup. Queue was 178.866 s; execution was 59.277 s.
Full phase and cache/source identities are in
`evidence/worker3-dev-profile-rollout-20261010.md`.

## Selected task-E2E source-freshness result — 2026-10-10

The one explicitly selected assertion
`fleet-workload.task-environment-source-and-cache-freshness` passed on
`zz-joe-e2e-2`, job `d89d056b7dc14474a8de861b4c295eab`, attempt
`6986cd48744e460ab8d2a97e4f8f7555`. This was one check of 931, not a full
suite. It reused all 16 declared cache components on environment handle
`06318b61f53c4b3ca5cb7dc620b5702f`, returned generation 11 parked after clean
teardown, and passed the selected assertion in 1.572 s. Queue was 0.574 s;
worker execution was 1,027.839 s, including a 645.113 s compile phase and a
352.702 s test phase. This supplies live immutable-source/cache-freshness and
parked-resource evidence, but no cold/warm savings comparison. Toolchain/ABI
invalidation, canceled-descendant cleanup on the installed handler, stale
receipt rejection and authority rollout/readback remain open. Full/coalesced
E2E and publishing were not run. Full details are in the Benchday companion
evidence `task-e2e-cache-freshness-20261010.md`.

### Task-E2E scope cleanup follow-up — 2026-10-10

The selected `fleet-workload.task-environment-task-e2e-scope` assertion
passed, but the worker could not park the environment because it found an
undeclared retained source file. The old worker error omitted the path. The
queued retry was canceled; Livestack added bounded path diagnostics and staged
worker release `07d233d3`, which is not active yet because the worker had
accepted another ZZOPS-owned full test. The check remains open until that
release is active and a selected retry ends with clean environment teardown.
Details: `evidence/task-e2e-scope-cleanup-20261010.md`.

### Benchday Rust retained-source acceptance — 2026-10-10

The admitted Benchday caller reused one parked Rust environment across source
updates, workers 3 and 4 on the same host, and an optimized release-profile
check. The unchanged source inventory allowed incremental Cargo reuse; a
changed inventory triggered target-cache invalidation. Receipts show successful
checks and parked cleanup at generations 32–34. Queueing remained separate,
including an 82.651-second wait for the optimized check. See
`evidence/benchday-rust-retained-source-20261010.md`. Toolchain/ABI invalidation
and the broad authority/worker release remain open.

### Rust build-mode follow-up — 2026-10-10

Four admitted Rust builds reused one parked environment across debug and
release profiles. Same-mode repeat compile phases were 0.196 s and 0.233 s;
each request still had its own queue wait. The logs show profile-specific
builds, while cache compatibility remained unchanged across profile switches.
This supports output freshness and warm reuse, but does not establish a
separate build-mode invalidation or controlled savings estimate. Real
toolchain/ABI invalidation and the remaining task 3.2 controls remain open.
See `evidence/rust-build-mode-resume-20261010.md`.

### Benchday selected cleanup/relocation check — 2026-10-10

The admitted task-E2E handler passed the exact cleanup/relocation assertion on
updated Benchday source, reused all 16 cache components and parked the
environment after clean teardown. The request still waited 34.545 seconds in
queue and executed for 597.357 seconds. The assertion uses an isolated
integration fixture; it does not prove a production cross-host rollout. The
earlier source snapshot failed image preparation because it lacked a required
CPU-replay output directory; a later Benchday commit fixed that setup before
the passing retry. Livestack toolchain/ABI invalidation and rollout acceptance
remain open. See `evidence/task-e2e-cleanup-relocation-20261010.md`.
