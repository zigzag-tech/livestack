# Fleet identity facts

## Why

Livestack already sees Harmony nodes through capability descriptors and keeps registered workload workers in its authority store. Its public `/fleet` view groups Harmony nodes under a source `host_id`, which is a logical label and may differ between processes on one physical machine. Workload worker identity and physical host are also separate authority fields. Benchday cannot safely join either resource to a Benchday host by comparing labels, hostnames, addresses, or device ids.

## What Changes

- Add explicit, stable node identity and Benchday host configuration to Harmony capability reports.
- Have the Livestack fleet broker create `hosted_on` Fact v1 observations from fresh, explicit node reports and return them through an authenticated complete snapshot.
- Add an explicit Benchday host mapping to workload worker principals and expose current registered-worker facts from the workload authority.
- Preserve each Livestack authority id, snapshot generation and sequence, source observation time, scope, and TTL.
- Keep placement and dispatch behavior unchanged.

## Capabilities

### New Capabilities

- `fleet-identity-facts`: explicit, authenticated host identity observations for Harmony nodes and Livestack workload workers.

### Modified Capabilities

- None. Existing fleet membership, workload admission, and placement contracts retain ownership of their current behavior.

## Design Record

This realizes the identity boundary needed by `_plans/fleet-broker.md` §3.1 and §3.3. Those sections are stale on identity: `/fleet` exposes the logical `host_id` and peer address but no explicit Benchday host relation; the workload authority records a worker's physical host without a Benchday host mapping. The new `_plans/host-identity-graph.md` states which Livestack authority owns each observation and how its complete snapshots are fenced.

## Impact

Changes stay in the Livestack monorepo: Harmony node configuration and capability projection, fleet broker projection and authenticated route, workload principal configuration and registered-worker snapshot. Hub integration is defined in Benchday change `hosted-on-bridge`. This source change emits identity facts only; it does not choose a worker, grant a capability, or create a placement-ledger entry.
