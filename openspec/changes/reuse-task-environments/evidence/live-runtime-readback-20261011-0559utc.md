# Live runtime readback — 2026-10-11 05:59 UTC

Read-only authenticated Workload API read. No job was submitted, canceled, or
retried, and no worker, authority, service, quota, policy, or automatic-selection
setting changed.

## Task-E2E canary attempt and cleanup

The 05:57 UTC read found job `2e1f68076649457abc4e2e59cb537d6b` terminal
`succeeded`; its exact attempt `f2b1ef86f0954dd894f857361ec19f76` was `ended`
on `zz-joe-e2e-1`. Its original result finished at 01:39:40 UTC; this was a
fresh terminal-state and cleanup read, not a new run. The job selected only
`fleet-workload.task-environment-source-and-cache-freshness` (1 of 938), on
source commit `a0028adf79cc5335748bba161cb7f64b93ff78c4`. Its assertion passed,
teardown was clean, and the accepted handler release digest was
`742328d4da0a9b9424fe90ee2ae1cd7ae6db2f7155b7da506101ab372ea57331`.
Environment `06318b61f53c4b3ca5cb7dc620b5702f` was `parked` at generation 18
with `writer_attempt=null`; its zz-joe replica reported 2,297,466,880 retained
bytes, with `reuse_outcome=reused` and reason `source_updated_incrementally`.
This clears the active-attempt/cleanup guard for that canary at this read time
only. Re-read both attempt and environment before any later target-worker
action.

## Worker roster

At 05:59:05 UTC, the authenticated roster reported:

- `zz-joe-e2e-1`: idle, ready, claim-enabled; advertises
  `benchday.e2e.task.v1`; unit state `behind`.
- `zz-joe-e2e-2`: running full/coalesced job
  `8d28eb3940d34c7faefb4549f797dcbf`, origin `system:test-train-dispatcher`.
- `zz-joe-e2e-3`: running image job `8e9f6377ff174bf5b457a15992111c65`.
- `zz-joe-e2e-4`: idle and claim-enabled, but does not advertise the task-E2E
  handler.
- `zz-joe-e2e-5`: idle but claim-disabled; it does not advertise the task-E2E
  handler.

The task-E2E-capable worker was idle in this snapshot, but its unit is behind.
The full/coalesced job and image job were left untouched; this read does not
provide capacity for or justify a duplicate task-E2E request.

## Release separation

The owner-reported daemon 3448 correction is recorded separately in
`release-status-correction-20261011.md`: its five builds are terminal succeeded
and it is no longer a hold. The same correction records the remaining owner-
reported 3450 Windows3 graph job. Neither release status substitutes for the
per-action task-E2E attempt/cleanup readback above.
