## 1. Registry

- [x] 1.1 Remove the per-handler release-count refusal and add the total release bound; verify a burst of eight releases for one handler is accepted and the old outcome name never appears (`test_burst_of_eight_releases…`).
- [x] 1.2 Share the reference-evidence query between retention and eviction; verify the existing constant-database-work collection test still passes unchanged.
- [x] 1.3 Add all-or-nothing capacity-driven eviction with a minimum age; verify oldest-first order, no eviction of default/rollback/job/worker-referenced releases, fresh releases protected, unset age disables, evidence outage evicts nothing, idempotent restage, racing stages stay within the byte bound.
- [x] 1.4 Verify eviction database work is independent of the number of releases (statement counts for 4 vs 14 releases).

## 2. Configuration

- [x] 2.1 Add `HandlerReleasePolicy` (extra=forbid, floor 3600) to the authority config schema and `burst_min_age_seconds` validation to the registry; verify bad values fail closed without echoing input.
- [x] 2.2 Surface the policy and limits in registry status.

## 3. Documentation and rollout

- [x] 3.1 Update `_plans/durable-workloads.md` hard bounds and the delta spec.
- [ ] 3.2 Deploy to the live authority with a backed-up state and config; verify health, worker reconnection, the new policy revision in registry events, and a scratch-handler burst with eviction; then archive this change.
