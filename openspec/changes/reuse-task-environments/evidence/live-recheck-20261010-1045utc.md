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

## Current-source replacement — 2026-10-10 10:56 UTC

Main advanced after the earlier request and changed Rust compilation inputs.
The queued job `7efa3d57f8e24fc0bbbcc15dc08754b1` had no attempts, so it was
withdrawn before starting. A current-main `check cli` request was accepted as
`5d83ba77da7749649452db71100b8cac` for source commit
`3062f53e79d89fc4e56b5b6d72bf09b8febd030b` and digest
`33ce58022b570b40a2b184b362a9e530d6e7c447efc9b1c539a2dd588aec7c48`.

Authority readback: the new job remains queued with no attempts; workers 1 and
2 are busy, workers 3 and 4 lack the environment profile, and worker 5 is
draining. The same environment remains parked at generation 19 with
18,510,524,416 retained bytes and no compute assigned. No full E2E, publish,
worker change, or service restart was made.

## Current-source compiler receipt — 2026-10-10 10:58 UTC

Current-main Benchday job `5d83ba77da7749649452db71100b8cac` succeeded on
`zz-joe-e2e-2` (attempt `a60d640fc4ca435e94c49ef274d287fa`) for source commit
`3062f53e79d89fc4e56b5b6d72bf09b8febd030b` and digest
`33ce58022b570b40a2b184b362a9e530d6e7c447efc9b1c539a2dd588aec7c48`.
Both `cargo-home` and `cargo-target` were reused; source reconciliation was
`source_updated_incrementally`. Measured phases were queue 36.500 s, transfer
7.690 s, source materialization 14.693 s, dependencies 0.462 s, compile
6.078 s, execution 17.392 s, and cleanup 0.019 s. Test timing was
`not_applicable`; the compiler exited 0.

The environment parked at generation 20 with 18,609,442,816 retained bytes
and no CPU/RAM assigned. The worker receipt reports 16.14 CPU seconds and a
1,320,431,616-byte memory peak. This proves current-source compiler success and
cache reuse, not a controlled speedup: the earlier older-source job spent
567.760 s in queue. Other source invalidation cases, worker/SDK rollout and
rollback proof remain open. No full E2E or publish was run.
