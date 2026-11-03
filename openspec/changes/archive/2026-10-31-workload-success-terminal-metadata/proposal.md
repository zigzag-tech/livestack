## Why

The authenticated workload authority publishes a successful intent as `done` without attribution or a reason, and the fetched result manifest omits the authority's terminal classification. The worker already supplies a supervised exit receipt, but WorkloadStore does not persist that evidence as part of the successful terminal record; the stream cannot report a verifiable success outcome.

## What Changes

- Validate successful completion against the worker's observed exit receipt: exit code zero and no resource-limit failure.
- Persist bounded success attribution and a stable reason code derived from that validated receipt with the terminal job record.
- Publish the same metadata on the `done` stream partial and include it in the owner-authenticated, hash-bound result manifest.
- Reject missing or conflicting success evidence as a completion error; do not infer success metadata from job state alone or change admission policy.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `harmony-workload-intents`: successful terminal outcomes must carry evidence-backed attribution and a bounded reason through the intent stream and fetched result.

## Impact

The worker completion payload and WorkloadStore completion transaction, `workload_authority.rung_of`, the result manifest and its authenticated route, plus their real-store and worker integration tests. The `_plans/durable-workloads.md` design record remains the owning design; it is stale because it documents worker result/artifact acknowledgement but not the success evidence required by the intent stream or result manifest.
