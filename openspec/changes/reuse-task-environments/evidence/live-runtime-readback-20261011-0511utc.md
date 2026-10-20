# Current worker and environment readback — 2026-10-11 05:11 UTC

Read-only authenticated Workload API snapshot; the roster's own timestamp is
`2026-10-11T05:11:53Z`. No job was submitted/canceled and no worker, authority,
rollout, service, or policy state was changed.

## Current Joe E2E workers

- `zz-joe-e2e-1` is running image job
  `7e8ebe2e123f40eb84f2f7a1df314098` (`benchday.compilation.image.v1`).
- `zz-joe-e2e-2` is running coalesced full-E2E job
  `8d28eb3940d34c7faefb4549f797dcbf` from `system:test-train-dispatcher`.
- Workers 3 and 4 are idle and do not advertise `benchday.e2e.task.v1`.
  Worker 5 is idle with claims disabled and also lacks that handler.
- Thus both workers currently advertising task-E2E are occupied by other
  workloads. The full E2E remains under the coalesced train; it was not run or
  modified here.

## Canary guard and retained handles

- Canary job `2e1f68076649457abc4e2e59cb537d6b` remains `succeeded`; attempt
  `f2b1ef86f0954dd894f857361ec19f76` is `ended` on worker 1.
- Its environment `06318b61f53c4b3ca5cb7dc620b5702f` is parked at generation
  18, with `writer_attempt=null` and 2,297,466,880 retained bytes.
- This read keeps the attempt/cleanup guard separate from terminal daemon
  3448 status. It confirms only this canary's cleanup state; any later
  target-worker mutation still requires a fresh active-attempt and cleanup
  read.
- Rust environment `4636512da5084b70bc2f0a1b0f045020` is marked parked at
  generation 47 with no writer but has no reported replica and last outcome
  `rebuilt`. It is not current evidence of a warm replica or a cache hit.

No automatic-selection, worker-rollout, or canary policy change is authorized
by this readback. The owner-reported daemon-3450 Windows3 job remains separate
and untouched.
