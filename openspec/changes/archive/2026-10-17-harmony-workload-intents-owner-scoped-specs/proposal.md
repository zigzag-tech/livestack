## Why

The native workload authority already has a bounded CAS resolver in its service startup path, but that behavior has no source-matched integration proof and the operator reference still says references are unconfigured. A real framed-ingress test should establish that authenticated intent identity reaches strict WorkloadStore admission and that every terminal outcome is attributed by the authority before Benchday removes its legacy job HTTP path.

## What Changes

- Define the Livestack contract for one-per-realm workload intents, owner-scoped request references, strict job admission, idempotency and attributed terminal outcomes.
- Exercise the production startup wiring with real WorkloadStore and BlobStore instances behind isolated fake framed ingress, covering accepted, refused, unavailable, replay and cancellation paths.
- Reconcile the stale `spec_resolver_unconfigured` note in `HARMONY.md` and `_plans/durable-workloads.md` with source-matched evidence; do not change production service state.

## Capabilities

### New Capabilities

- `harmony-workload-intents`: authenticated, durable, owner-scoped workload intents and authority-published outcomes.

### Modified Capabilities

None.

## Impact

`node-py/livestack_node/hostd_streams.py`, `node-py/livestack_node/workloads/service.py`, `node-py/livestack_node/streams/workload_authority.py`, framed-ingress tests, `HARMONY.md` and `_plans/durable-workloads.md`. The design record `_plans/durable-workloads.md` remains authoritative for durable job ownership and strict admission; its description of the intent adapter is incomplete, and the operator gap note is stale because service startup already provides the BlobStore resolver.

No endpoint is enabled or restarted by this change, and no test contacts a production authority or worker.
