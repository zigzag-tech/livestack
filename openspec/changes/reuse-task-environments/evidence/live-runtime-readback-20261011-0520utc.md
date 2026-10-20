# Live runtime readback — 2026-10-11 05:20 UTC

Read-only authenticated Workload API and ZZOPS status checks. No workload was
submitted, canceled, or retried, and no worker, authority, unit, policy, quota,
or automatic-selection setting changed.

## Worker roster

- `zz-joe-e2e-1` is ready and claim-enabled, but busy running image job
  `7e8ebe2e123f40eb84f2f7a1df314098`, attempt
  `27f62e08780f4fbeb9704048702ff175`, handler
  `benchday.compilation.image.v1`. Its unit reports `behind`.
- `zz-joe-e2e-2` is ready and claim-enabled, running full-E2E job
  `8d28eb3940d34c7faefb4549f797dcbf`, origin
  `system:test-train-dispatcher`. Its unit state is `unknown`.
- `zz-joe-e2e-3` and `zz-joe-e2e-4` are idle Rust/Flutter workers but do not
  advertise `benchday.e2e.task.v1`; worker 5 is idle with claims disabled.

The independent ZZOPS status has no pending cargo and reports one dispatched
full completion train, `tt_12b3f363-ca31-4f04-b830-a59e0ba7d31b`, still running
on job `8d28eb3940d34c7faefb4549f797dcbf` on worker 2. This is the coalesced
full run; it was neither started nor modified for this change.

The Workload API `get` endpoint returned `job not found` for that full-run job
ID, while the current worker roster and ZZOPS status both report it running.
Treat the job as active; the isolated `get` 404 is not terminal evidence.

## Canary cleanup guard

Canary environment `06318b61f53c4b3ca5cb7dc620b5702f` is parked at generation
18 with `writer_attempt=null`. Its replica on `zz-joe` is parked with
2,297,466,880 bytes retained and last outcome `reused`. This confirms the
cleanup state for that canary only. Before any later target-worker action,
re-read the active attempt and cleanup state for that target; this guard is
independent of daemon 3448's terminal status.

## Release and action boundary

The owner-confirmed release correction remains in
`release-status-correction-20261011.md`: daemon 3448 is terminal and no longer
a worker-drain hold; root retry 3450 has only its Windows3 graph job remaining
and is to be preserved without restart or duplication.

No task-specific E2E job was queued because the two workers advertising that
handler are occupied. No full/coalesced E2E, build, publish, worker restart, or
operator rollout action was initiated by this readback.
