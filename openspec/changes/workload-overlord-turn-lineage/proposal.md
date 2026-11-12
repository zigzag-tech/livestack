## Why

Benchday's client graph needs an exact path from an authenticated Overlord turn to an owner-backed workload job, attempt, worker session and current explanation. The owner-bound job source currently lacks a turn source id and worker boot identity, so consumers cannot make that join without unsafe guesses from labels or timing; `_plans/durable-workloads.md`'s three-application owner model needs this explicit lineage contract.

## What Changes

- Accept an optional typed Overlord-turn source id from the trusted submitting service, validate and persist it as immutable job provenance, and keep it separate from caller labels.
- Extend the fixed-owner incremental job reader with an exact turn-id filter and the matched source id, authority-issued attempt id, worker id and worker boot session id.
- Return a bounded, structured current explanation derived from authority-owned job state and stable reason codes; do not expose handler prose, command logs, artifact contents or credentials.
- Extend the WorkloadClient response validation and document owner scope, field provenance, bounds and compatibility.

## Capabilities

### New Capabilities

- `workload-turn-lineage`: exact owner-backed turn-to-job and current-attempt provenance for read-only graph joins.

### Modified Capabilities

- `harmony-workload-intents`: optionally accept and preserve a validated source turn id on a workload submission without changing owner or admission authority.

## Impact

This realizes the stale lineage gap in `_plans/durable-workloads.md`'s “Three applications, one authority” record. It affects the WorkloadStore job schema/migration and submit validation, the fixed-owner job-change route and `WorkloadClient`, focused `node-py/tests/` coverage, `HARMONY.md`, and that design record. No placement decision or workload mutation is added to the read-only job-reader role.
