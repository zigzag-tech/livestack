# Durable Harmony workloads

Status: implementing; companion integration proposal is Benchday
`openspec/changes/harmony-build-test-capacity`. Approved 2026-09-08.

Harmony owns generic workload admission, resource claims and attempt execution.
Benchday owns E2E selection/verdicts and release stage dependencies/signing policy.
The workload service is separate from inference admission: it never converts an
exception into permission and never invokes GPU load/evict actions.

The service persists jobs, attempts, workers and claims in SQLite WAL. Submission
is principal-scoped and idempotent. Admission takes an immediate transaction,
reuses `fleet_scheduler.schedule`, and persists the attempt before returning it.
All worker kinds on a physical host share the resource budget. A worker reports
both configured capacity and measured headroom; absent dimensions cannot grant.
Workers are authenticated, handlers are installed/allowlisted, and job payloads
cannot supply shell commands. Every attempt carries a fence and input digest.

Restart marks workers unready until reconciliation. Expired claims fence results
but retain capacity until the worker confirms owned-process cleanup. A disconnected
host cannot receive another attempt; another host can retry infrastructure work.
Product failure is terminal. Worker-owned heartbeats outlive caller sessions.

Source objects are SHA-256 addressed. A worker verifies them before extraction
into a private source root and writes only to separate output state. Dependencies,
submodules, modes and dirty source provenance belong in the manifest; development
checkout symlinks and secrets do not. Content and log stores enforce byte limits.

Bounds: 1,000 active jobs; 10,000 retained terminal jobs or 14 days; 3 attempts/job;
128 workers; 32 active claims/worker; 64 KiB job/result records. Cleanup runs during
transactions and periodic service sweeps. Referenced input remains pinned; hard
caps reject new admission if nothing can be reclaimed. Retention exemptions are
honored. Unconfigured destructive retention disables deletion, not admission caps.
Service logs use rotation; SQLite WAL checkpoints constrain journal growth.

Rollout: isolated store/process failure tests, two real supervised workers, remote
E2E, then release stages. Win One is WSL Ubuntu via SSH port 2222 for provisioning;
normal worker traffic is outbound HTTP, with no caller-owned SSH session.
