## 1. Authenticated stream consumer

- [x] 1.1 Implement the account-verified Benchday Hub subscription and bounded `harmony.fleet/1` event consumer; verify command identity, `subject.machine` identity, complete-coverage staging, warm-receipt staleness, and the 256-partial / 4,096-byte / 1-MiB-per-generation / 512-partial / 2-MiB-total bounds in stream-consumer integration tests. Ledger obligation: none; this path only observes facts.
- [x] 1.2 Rewire `fleetTick` and the `read_fleet_view` handback to the stream observation, remove `FleetClient.view()` and its `/fleet` route, and verify unknown/stale/degraded/gap each block `/fleet/plan` while fresh live coverage reaches it. Ledger obligation: unchanged; placement decisions remain on `/fleet/plan` and operation records remain on the existing broker ledger.

## 2. Recovery, docs and checks

- [x] 2.1 Add stream-source-to-tick integration coverage for account mismatch, refusal, reset, incomplete coverage, recovery after a complete live cut, warm-cache refusal, replacement bounds, and absence of HTTP `/fleet` fallback; verify all four named outcomes remain distinct. Ledger obligation: none; no placement behavior is added.
- [x] 2.2 Document the expected account ID, Hub envelope identity, warm-cache behavior, resident credentials and stream source lifecycle; verify setup documentation matches the CLI flags and required configuration. Ledger obligation: none; documentation only.
- [x] 2.3 Run `npm test`, `npm run typecheck`, and `openspec validate --specs`; record each result before landing. Ledger obligation: confirm the existing `/fleet/plan` and `/fleet/operations` paths remain unchanged and retain their existing ledger behavior.

Final-tree receipts are in `evidence/final-tree-20261031.md`. The wall-clock visibility scenario mentioned in an earlier review is already covered by current `origin/main`; this change adds no duplicate wall-clock delta.
