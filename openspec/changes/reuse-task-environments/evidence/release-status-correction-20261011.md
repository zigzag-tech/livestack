# Release and canary status correction — 2026-10-11

This records the owner's latest release correction and the separate live
canary readback. Owner-reported release-plan state and authenticated workload
state are kept distinct.

## Daemon 3448

- The owner confirms all five target builds are terminal **SUCCEEDED**.
- Publication failed only on artifact read with HTTP 404. The namespace fault
  is fixed; the full 47 MB artifact and a foreign-404 control passed.
- Release 3448 is no longer an active-build or worker-drain hold. This fact
  does not waive any task-E2E attempt/cleanup guard.

## Root retry 3450 — terminal completion

- The owner confirms plan `78d0358d-8ad1-4395-96e6-dbd374144caa`, graph
  `graph-target-v1-e9a7050f7c833d9de10a0211`, succeeded on all five targets.
- Publication completed under train `20261011T033118Z-90012`.
- This supersedes the earlier report that Windows3 job
  `2a188eab8e214b66b07b8c668e09b0aa` was still running (root wait session
  `31405`). No active 3450 build or worker-drain hold remains.
- The earlier non-graph-qualified Workload API lookup was not used to establish
  terminal graph status; this correction records the owner's terminal update.

## Independent task-E2E attempt/cleanup guard

The guard remains a required, separate precondition for any target-worker
action. An authenticated Workload API read at `2026-10-11T04:53:42Z` found:

- Job `2e1f68076649457abc4e2e59cb537d6b`: `succeeded`.
- Attempt `f2b1ef86f0954dd894f857361ec19f76`: `ended` on `zz-joe-e2e-1`.
- Handle `06318b61f53c4b3ca5cb7dc620b5702f`: parked at generation 18,
  `writer_attempt=null`; worker 1 was idle, ready and claim-enabled.

This read clears the attempt/cleanup guard for that exact canary only. The
guard itself remains in force: daemon 3448's terminal state cannot clear it,
and any later target-worker action requires a fresh authoritative attempt and
cleanup read.

## Changes and review boundary

No worker, authority, policy, quota, service, handler, or automatic-selection
state was changed for this correction. The explicit ARM Rust caller/handler,
exact `rustc -vV` target check, Rust-only permission intersection, existing
`xc-mac-studio` physical-host budget, and 8 GiB workspace + 16 GiB quota-image
design are in the paired OpenSpec changes for owner review. Implementation,
worker enrollment, and automatic selection remain pending that review and
their acceptance gates.
