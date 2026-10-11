## Why

Benchday needs a bounded incremental view of Harmony workload changes, but the existing endpoint accepts an admin bearer and can return every owner's jobs. A dedicated fixed-owner, read-only service grant lets a colocated producer observe one owner's jobs without granting workload administration or cross-owner access.

## What Changes

- Add a `job_reader` principal role whose credential is bound to its principal id and can access only the owner-scoped job-change read route.
- Add a bounded incremental job-change route that derives its owner from the authenticated `job_reader`, rejects owner selection, and supports safe pagination.
- Preserve the existing admin job-change route and workload mutation behavior.

## Capabilities

### New Capabilities
- `workload-job-source`: owner-bound read-only credentials and a bounded incremental job-change API for colocated stream producers.

### Modified Capabilities
- None.

## Impact

This realizes `_plans/durable-workloads.md`'s “Three applications, one authority” owner model. That section describes effective `labels.owner` and the caller's recent job list, but it does not specify an incremental source or a fixed-owner read-only credential; the current admin-only change route is an implementation beyond that plan. The change affects workload principal validation and routing, the job-change projection/client, API documentation, and focused tests under `node-py/`. It adds no durable state, placement decisions, or decision-ledger entries. The existing listener configuration remains operator-owned; consumers require a loopback-compatible bind.
