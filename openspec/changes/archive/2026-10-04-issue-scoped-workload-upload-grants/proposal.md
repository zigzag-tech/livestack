## Why

ZZOPS must let a source publisher place one immutable object in the workload owner's CAS without giving that publisher a workload caller token that can also submit jobs. This is needed to keep large source archives off the remote ZZOPS coordinator while preserving the existing owner-scoped workload input model.

## What Changes

- Add an operator-configured permission for a workload principal to mint narrowly scoped upload grants for its own object namespace.
- Bind each grant to an idempotency key, one SHA-256 digest, exact byte size, and a short expiry; keep only the capability hash and bounded receipt state in durable storage.
- Let the holder stream the exact object directly to the authority using the opaque grant. The grant cannot read objects, submit or observe jobs, create another grant, or upload a different digest or size.
- Provide a small owner-authorized status response so the coordinator can verify the object receipt without downloading the object.
- Record grant issue, refusal, expiry, and successful upload as bounded workload audit events without logging capability material.

## Capabilities

### New Capabilities
- `workload-upload-grants`: one-object, owner-scoped, expiring upload authorization that is separate from job submission and ordinary caller credentials.

### Modified Capabilities

## Impact

- `_plans/durable-workloads.md`: the normal transfer protocol remains authoritative, but its current caller-token assumption is stale for source publishers that must not submit jobs.
- `node-py/livestack_node/workloads/`: workload principal configuration, authority routes, object CAS ownership and transfer client.
- `node-py/tests/`: real HTTP, SQLite durability, exact-size/digest refusal, one-grant scope, retry and principal-isolation coverage.
- The ZZOPS source publisher will consume this API in its companion repository; that consumer change remains outside this LiveStack change.
