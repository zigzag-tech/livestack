# Host identity graph

**Status:** DESIGN, producer implementation tracked by OpenSpec change
`hosted-on-identity-facts` (2026-10-06).

## Purpose

Publish only source-owned, expiring identity joins between compute resources and
explicit Benchday host ids. This is metadata for correlation and display; it
does not schedule work or grant capabilities.

## Current gap

`_plans/fleet-broker.md` §3.1 and §3.3 describe the fleet view as groups of
nodes under a logical `host_id`. That value may differ between processes on
one physical machine, while `device_id` names a placement device. The view
does not currently expose a stable resource id or a Benchday host relation.

The workload authority binds a worker credential to a worker id and physical
host and stores its latest registration in a bounded SQLite table. It has no
explicit Benchday host mapping. Neither source may fill the gap by comparing
ids, hostnames, URLs, or device labels.

## Source contracts

### Harmony fleet broker

Each Harmony node may be configured with:
- `LIVESTACK_IDENTITY_NODE_ID`: a stable, operator-assigned id for the node;
- `BENCHDAY_HOST_ID`: the corresponding explicit Benchday host id.

When both values validate, the node's capability response includes these
explicit ids and a present status. The node does not create a Fact or assign
snapshot fences. The fleet broker uses its last successful capability
observation to create a Fact v1 edge from `harmony:node:<node-id>` to
`benchday:host:<host-id>`. It omits stale reports and ambiguous node ids.
The Fact authority is the fleet broker's configured
`LIVESTACK_IDENTITY_AUTHORITY_ID`; the broker assigns the process generation
and increasing sequence for each complete snapshot. The Fact observation time
is the last successful capability read and its TTL is at most 60 seconds.

### Livestack workload workers

A worker's principal may carry an optional `benchday_host_id`, owned by the
workload authority configuration. Worker reports cannot set or change this
field. The authority publishes a Fact only for a fresh worker registration
that has this explicit mapping. It uses the worker id from its principal to
form `livestack:worker:<worker-id>`; the principal mapping supplies the
`benchday:host:<host-id>` destination. The workload authority id must be
explicitly configured; without it the endpoint refuses the snapshot.

### Snapshot and trust boundary

Each endpoint is authenticated and returns a bounded complete snapshot with a
source generation and increasing sequence. A higher sequence replaces the
prior source cut atomically; a new generation fences snapshots from a restarted
source. Facts retain their source authority and Fact v1 fences. The Benchday
adapter binds each endpoint credential to the expected authority, scope, and
source id namespace.

## Decision ledger

Identity endpoints are read-only and do not invoke the planner, scheduler, or
worker dispatch. They create no decision-ledger record. Verification checks
that a read changes neither placements nor the ledger.

## Rollout

Keep existing `/fleet`, capability, workload registration, and dispatch
contracts compatible. Configure one test Harmony node and one test workload
principal before enabling Benchday ingestion. Unconfigured resources remain
visible through current views and resolve to unknown in Benchday.
