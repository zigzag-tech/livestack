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
- [x] 3.2 Deployed 2026-10-05 13:43 UTC (livestack 4932b47e): state+config backed up and verified, release dir `…+burstheadroom4932b47e`, drop-in `99-zz-burst-headroom.conf`, restart 1.3 s, status shows policy revision `benchday-e2e-handler-release-burst-20261005` with burst_min_age_seconds=3600 and no per-handler limit; burst of 8 + eviction proved with the deployed code on a scratch handler in a throwaway authority; zzops/hub healthy afterwards. Open: first production eviction event and archive.

## 4. Post-deploy finding (2026-10-05)

- [x] 4.1 The overlay release the authority runs (an older patch stack) had `node-py/livestack_node/workloads/store.py` using `re.fullmatch` in worker registration with no `import re` (origin/main has it). Registrations carrying a handler inventory failed with HTTP 503 (`NameError`, about 3,000 log lines, workers `zz-joe-e2e-1/2` and `xc-win-1-wsl` not ready). The bug predates this change (the previous release had the same file) but the deploy carried it forward and its checks did not exercise registration. Fixed in the deployed release (`…+burstheadroom4932b47e`, one-line `import re`, backup `store.py.before-import-re`), restart 13:50 UTC: zero new errors, 15 of 15 fresh workers ready, e2e workers at registry generation 7.
- [ ] 4.2 Overlay releases are hand-patched copies; add a deploy check that runs pyflakes (undefined names) over the deployed `workloads/` directory and verifies a real worker registration (a worker reports ready with a handler inventory) before declaring a deploy healthy.
