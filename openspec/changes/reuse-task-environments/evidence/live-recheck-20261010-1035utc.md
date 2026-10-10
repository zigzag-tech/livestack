# Task environment live recheck — 2026-10-10 10:35 UTC

The workload authority process on `xc-tower-ubuntu` is using the
`livestack-486cf6e5` release, which includes the upload-capacity preflight. A
fresh authenticated capability read reports schema versions 1–4; environment
API v1 allows the Flutter, Rust, and task-specific E2E handlers and forbids
full E2E, dependency, commerce, and release handlers.

The existing Rust development environment remains parked at generation 18,
with last outcome `reused`, on `zz-joe`. A Benchday source submission using
that handle was accepted as job `44e6910403f14abeb8bf7042a4dcb96e` with source
digest `5d18841deaed4fd0dbf942865d9441426830f86e5dad178e1972bbc1643422d8`.
This shows that the source bundle now reaches job admission. The job is still
queued with no attempts, so this does not yet prove source materialization,
mirror reconciliation, invalidation, or compiler reuse.

The scheduler reports workers 1 and 2 busy, workers 3 and 4 without the
environment profile, and worker 5 draining. The existing environment remains
parked and the queued request has no CPU/RAM assignment. No new source-mirror
or compiler result is available yet.

No service restart, worker/configuration change, cancellation, full/coalesced
E2E request, or publish was made during this recheck.
