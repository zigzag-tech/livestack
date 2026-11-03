# Tasks

- [x] 1. Add `device_leases.py` and `test_device_leases.py` with strict validation, durable transitions, idempotency, TTL, and restart recovery.
- [x] 2. Add immutable device lease state to snapshots and fence ordinary planner/warm/restore paths while preserving static exclusive-unit behavior.
- [x] 3. Implement broker acquire/reconcile with managed eviction, allocator reclaim, measured verification, bounded waits, expiry cleanup, and fleet/status evidence.
- [x] 4. Extend host `/admit`, heartbeat/release, and client APIs with owner-checked exclusive lifecycle and no degraded exclusive grants.
- [x] 5. Add planner, broker, HTTP, and client tests, preserving existing imagegen behavior and adding owner lease-forwarding coverage.
- [x] 6. Verify a real isolated two-peer HTTP lifecycle: co-residency, drain/wait, memory blocker, owner-only admission, heartbeat/release, and restart/expiry.
- [x] 7. Run focused and full qualified test suites, compare every full-suite failure with unchanged `HEAD`, and require no new regressions. Record the 74 pre-existing workload/Docker/systemd baseline failures separately. Run `openspec validate add-exclusive-device-leases --strict` and `git diff --check`. Do not activate production or retry image generation before the focused suite passes and baseline equivalence is established.
- [x] 8. Add ordered multi-device lease acquisition, all-device owner validation, primary-only placement, local CUDA meter peers, and paired result evidence.
- [x] 9. Qualify two actual 768×432 INT8 transformer + INT8 ControlNet generations across two GPUs, inspect geometry fidelity, and retain inputs, outputs, per-device peaks, leases, hashes, and limitations.
