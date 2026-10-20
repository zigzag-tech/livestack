# Authority activation readback

Operator-provided verified outcome, 2026-10-11. The exact activation and
readback timestamp was not provided.

## Activated authority candidate

- Candidate release: `livestack-c2930611`, commit
  `c2930611666a02a6db74877ec841fc113df4b52b`.
- Candidate path:
  `/home/ubuntu/.local/share/livestack-workload-releases/livestack-c2930611`.
- The operator independently verified the 409-file package digest:
  `da2bc5dfd3b3cf841aaf514ec0e04075b1ed19b10080816b4c8fd756c3861389`.
- The 258-file code manifest digest is
  `93fcb848aed039930378494811f662a694800e4da2af476c00cd88dee73e2f54`.
- Activation command:
  `bash /home/ubuntu/livestack/tools/deploy-authority-release.sh /home/ubuntu/.local/share/livestack-workload-releases/livestack-c2930611`.
- The operator reports static checks, throwaway boot, worker registration,
  job round trip, and backup integrity all PASS. Backup:
  `/home/ubuntu/.local/state/livestack-workloads/backups/release-20261011T011930Z`.
- The live service points to the exact release path
  `/home/ubuntu/.local/share/livestack-workload-releases/livestack-c2930611/node-py`.
- The operator reports 22/22 workers ready after 6 seconds. This is service
  readiness; per-worker task-environment capabilities and quotas were not
  included in this readback.
- Activation log on `xc-tower-ubuntu`:
  `/tmp/infra-taskenv-authority-activation.log`.

## Fence and accepted work

The operator reports the ZZOPS fence drain completed in `abort` mode, with
`drained=true`, 13 ms wait, and no stragglers. The Benchday and Askafox holds
were then released through the normal `fence-release` operation with
`force=false`. Accepted work was preserved. Its current queue state was not
read back here.

The activation used the user-appointed operator's accepted account claim
`infra-13cdfe30-a7f8-4c37-9432-1ed9ed832ebd`; the operator reports that claim
was released. No worker rollout, rollout-spec change, quota change, or
automatic-selection change was made.

## Remaining rollout acceptance

This records the authority activation only. Task 4.4 remains open. The
following post-activation results are not yet verified in this change:

- Current authenticated capability allow/forbidden lists and exact authority
  release provenance readback.
- Per-worker installed release, handler profiles, quotas, cleanup state, and
  supported SDK consumer coverage.
- A representative task-specific canary against the activated authority and
  supported workers.
- Rollback proof/readback. Activation succeeded, so rollback was not exercised.
- Full live rollout/controller readback. Automatic environment selection
  remains disabled until that readback and the remaining acceptance pass.

The candidate's pre-activation job round trip and the 22/22 readiness report do
not substitute for these post-activation checks. No full/coalesced E2E run or
publish was performed for this change.

## Post-activation capability and rollout readback

Read-only authority and worker readback at `2026-10-11T01:23:18Z`:

- The authenticated Benchday caller reports environment capability version 1.
  Allowed handlers are Flutter check, Rust check, and task-specific E2E.
  Dependency E2E, full/coalesced E2E, commerce, and release/publishing handlers
  are forbidden.
- The authority roster has 27 registered worker rows and 22 ready workers.
  On `zz-joe`, workers 1 and 2 advertise Flutter, Rust, and task-E2E. Workers 3
  and 4 advertise Flutter/Rust but not task-E2E. Worker 5 lacks task-E2E and
  claims are disabled.
- Handler registry generation 72 has task-E2E default digest
  `742328d4da0a9b9424fe90ee2ae1cd7ae6db2f7155b7da506101ab372ea57331`.
  Workers 1 and 2 report it installed with no activation failures. Their latest
  handler-release GC receipts are complete; these are not environment cleanup
  receipts.
- The rollout controller remains in `observe`, spec generation 2, unit
  `unit-f38a7baa`, report generation 6019. Its report says worker 1 is behind
  and workers 2–5 are unknown; it proposed observed-only actions for worker 1
  and applied none.
- Workers 1–5 have matching configured quota bounds: 32 GiB per replica,
  128 GiB per owner, 124 GiB host aggregate, and a 1 GiB reserve. Only workers
  1 and 2 list the task-E2E environment profile. Exact project-quota usage and
  parked cleanup for the active canary handle remain pending.

During the canary attempt, a direct read of the `zz-joe` project-quota marker
and kernel quota for the existing handle confirmed generation 18, state
`running`, and an exact 32 GiB hard limit matching the marker. Kernel usage was
2,357,088,256 bytes; the marker's last accounted use was 2,297,446,400 bytes.
This is an in-flight measurement, not the final parked usage or cleanup receipt.

## Post-activation canary in progress

One exact task-specific check is running through Benchday's
`scripts/task-e2e.mjs`:

- Job `2e1f68076649457abc4e2e59cb537d6b`.
- Check `fleet-workload.task-environment-source-and-cache-freshness` (1 of
  938).
- Existing handle `06318b61f53c4b3ca5cb7dc620b5702f`, generation 18.
- Captured source SHA-256
  `b390f5d165b945444b1be2af06dfd97fdef61e8ede6bb7b22f09c2e8bd9bc64b`.
- The authority roster places it on `zz-joe-e2e-1`; no terminal result has been
  observed yet. The pre-run handle was parked at generation 17 with
  `last_outcome=reused` and 2,297,446,400 retained bytes.

The first capture attempt from the shared checkout failed source-pin validation
before job submission because its external Jingway link resolved to a different
commit than `packages/JINGWAY.lock`. The retry used the task worktree, whose
pinned Jingway link matches the lock. No other repository, dependency checkout,
worker, or service was changed to make the retry.

The controller's stale/unknown worker states and two-worker task-E2E enrollment
keep this rollout incomplete. Capability, quotas, cleanup, representative
canary completion, broader worker/SDK rollout, and rollback proof remain open.
## Terminal canary and post-job readback

This supersedes the earlier in-progress canary note. The runner finished at
2026-10-11T01:39:40.677Z; the readback below was taken at 01:44:22Z.

- Job 2e1f68076649457abc4e2e59cb537d6b, attempt
  f2b1ef86f0954dd894f857361ec19f76, succeeded on zz-joe-e2e-1. It selected
  fleet-workload.task-environment-source-and-cache-freshness (1 of 938);
  handler exit was 0, the assertion passed in 1,500 ms, and teardown left no
  containers, networks, or volumes.
- Handle 06318b61f53c4b3ca5cb7dc620b5702f advanced from parked generation 17
  to 18. The receipt reports source_updated_incrementally and all 16 cache
  components reused. Queue was 1.765 s; source materialization 17.131 s;
  dependency preparation 0 s; compile 299.508 s; test 321.953 s; execution
  662.440 s; transfer 6.925 s; cleanup 0.007 s. Phases overlap and are not
  additive. There was no paired cold run, so this does not quantify saved time.
- The harness reported startupBudget failed at 150,346 ms against 60,000 ms;
  the selected assertion and disposable run verdict passed.
- The parked environment retained 2,297,466,880 bytes. Its on-host marker and
  kernel project quota matched: project 100481 on /dev/loop1, hard limit
  2,834,337,792 bytes, usage 2,297,466,880 bytes. The attempt systemd unit
  was inactive with MainPID 0.
- Capability v1 allowed only Flutter check, Rust check, and task-specific E2E.
  Dependency/full E2E, commerce, and release/publishing handlers were
  forbidden. Automatic selection remained disabled.

This snapshot showed only Joe workers 1 and 2 advertising task-E2E and an
observe-only controller with worker 1 behind, workers 2–5 unknown, and no
applied actions. Backup integrity and the previous release pointer were
verified; production rollback was not exercised. The fresher 01:56 UTC
capability, worker, controller, canary-artifact and rollback-branch readback is
in post-activation-readback-20261011-0156utc.md. Broad worker/SDK rollout,
production rollback acceptance, and remaining cache compatibility work remain
open. No full/coalesced E2E or publish was run.
