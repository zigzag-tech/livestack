# Task environment Rust check — 2026-10-10 10:45 UTC

The workload authority remains on `livestack-486cf6e5`. Its authenticated
capability response allows the Flutter, Rust, and task-specific E2E handlers;
full E2E, dependency, commerce, and release handlers remain forbidden.

Benchday job `44e6910403f14abeb8bf7042a4dcb96e` succeeded on `zz-joe-e2e-2`
with both Rust cache components reused. The receipt reports
`source_updated_incrementally`, then parked generation 19 after cleanup.
Measured phases were queue 567.760 s, transfer 5.670 s, source materialization
17.276 s, dependencies 0.471 s, compilation 2.102 s, execution 9.433 s, and
cleanup 0.006 s. This verifies a real admitted compile and reuse after source
upload; the source commit was older than current Benchday main, and this is not
a controlled cold/warm speedup comparison.

Current-main Benchday job `7efa3d57f8e24fc0bbbcc15dc08754b1` has also been
accepted using the same environment, but it remains queued with no attempt.
Workers 1 and 2 are busy, workers 3 and 4 lack the environment profile, and
worker 5 is draining. The environment remains parked with no CPU/RAM assigned
to the queued request. Current-main source freshness and compilation results
are pending.

The source mirror edit/deletion/symlink and lockfile/toolchain/ABI invalidation
sequence is still incomplete. Broad worker rollout, cleanup/rollback readback,
and SDK provenance are also still open. No service restart, worker/config
change, cancellation, full E2E, or publish was made.
