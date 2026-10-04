## Why

Harmony's workload authority and workers load handler executables from startup configuration, so a product-level handler change currently requires rewriting worker setup and waiting for idle restarts. This change lets compatible handler releases move through a validated registry while the same authority and worker sessions continue serving accepted work.

## What Changes

- Add immutable, content-addressed handler packages with a versioned execution contract and bounded staging, installation, retention, and garbage collection.
- Add authenticated live registration, compare-and-swap activation, rollback, and receipts that distinguish desired, effective, and worker-observed generations.
- Resolve a handler release at job acceptance, retain submitted idempotency intent separately, and preserve exact release identity through placement, retries, execution, completion, and recovery.
- Let workers install compatible packages on their existing control loop, advertise exact installed releases, and activate a new registry generation without changing PID, boot identity, or active leases.
- Record release staging, activation, placement, recovery, and retirement decisions in the workload decision ledger.

## Capabilities

### New Capabilities

- `workload-handler-releases`: operator-managed immutable handler packages, live registry generations, release-aware acceptance and placement, worker activation, result identity, and bounded reference-safe retention.

### Modified Capabilities

None. The workload authority has no current OpenSpec capability; this change creates its current-truth contract.

## Impact

The paired Benchday consumer contract is `benchday/openspec/changes/harmony-handler-hot-activation`. This owner-side change covers `node-py/livestack_node/workloads/` and its HTTP/SQLite/CAS/worker integration tests, the operator reference in `HARMONY.md`, and `_plans/durable-workloads.md`. That plan currently treats the handler catalogue as startup-loaded configuration and assignments as name-only; those statements become stale for release-aware handlers. Existing name-only callers and configured handlers remain an explicit legacy path during migration.
