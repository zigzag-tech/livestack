# Design: fleet identity facts

## Context

The fleet broker's `/fleet` view includes peer addresses, device ids, readiness, and logical host grouping. Harmony's node identity can be a mesh identity or a host-and-port fallback, neither of which is a suitable durable source id for every deployment. The workload authority binds each worker credential to a worker id and physical host; its SQLite worker table records current registration state. Neither system currently publishes an explicit Benchday host relation.

Livestack owns two source authorities: the fleet broker owns aggregate Harmony node facts based on successful capability reads, and the workload authority owns worker facts based on its registered worker rows and principal configuration. Benchday owns account binding, Fact admission, current-edge resolution, and roster projection.

## Goals and Non-Goals

### Goals

- Require explicit, stable resource ids and explicit Benchday host ids.
- Publish Fact v1 `hosted_on` edges without deriving relationships from labels or addresses.
- Authenticate source reads and preserve each authority's snapshot generation and sequence.
- Publish a complete, bounded snapshot so a newer cut replaces an older cut atomically.
- Keep all placement and admission behavior unchanged.

### Non-Goals

- Change the planner, scheduler, router, or workload dispatch.
- Treat Livestack's logical `host_id` or a device id as a Benchday host id.
- Add durable identity storage beyond existing workload worker registration state.
- Add a decision-ledger event for a read-only identity snapshot.

## Decisions

### The fleet broker owns Harmony node snapshot facts

A node may be configured with `LIVESTACK_IDENTITY_NODE_ID` and `BENCHDAY_HOST_ID`. Both values must validate. The capability response carries those explicit fields and an identity status; it does not create or sequence Facts itself. The node id is operator-assigned and stable for that node process. It does not reuse the peer URL, mesh address, or logical `host_id`.

The fleet broker consumes its last successful capability observations. It creates one Fact v1 edge for each fresh, unambiguous node id and host mapping. Conflicting mappings for the same node id are omitted and counted. The Fact authority is the fleet broker's explicitly configured `LIVESTACK_IDENTITY_AUTHORITY_ID`; its process generation and increasing snapshot sequence fence the complete cut. Each Fact's observation time comes from the last successful capability read, not from an endpoint poll. A stale observation is omitted after the 60-second bound.

`GET /fleet/identity-facts` uses the existing fleet principal table and returns the whole current cut plus named source counts. If the authority id or auth table is unavailable, the endpoint refuses the read with a named 503. Missing or invalid callers receive the normal authentication refusal.

Alternative considered: have the node create and forward a Fact with its own authority and fences. Rejected because the fleet broker already owns the aggregate snapshot and can provide one atomic cut; copying node-local fences into a broker cut would mix independent source generations. Synthesizing an edge from `host_id`, peer URL, or device id is also rejected because those values do not establish physical host identity.

### Worker mappings belong to the authenticated workload principal

Add optional `benchday_host_id` to worker principals in the workload authority's mode-protected configuration. Worker reports do not carry this field. The authority joins registered worker ids from its existing bounded `workers` table to the principal mapping. It only emits a fact for a fresh registration. Principal reload treats changes to this mapping as an identity binding change; an operator removes the old principal and adds the new mapping so old worker state cannot silently change owners.

The workload identity endpoint is authorized to admin principals. It creates Livestack authority Facts for the registered worker mappings using an explicitly configured `identity_authority_id`, a service generation, increasing snapshot sequence, the registration's observation time, and a TTL no greater than the configured freshness bound. If the authority id is unset or invalid, the endpoint fails closed with a named 503. The worker query reads at most the existing configured worker limit in one statement.

Alternative considered: accept `benchday_host_id` in the worker report. Rejected because a worker report must not be allowed to choose its own host authority mapping.

### Snapshot semantics handle reassignment

Each endpoint returns all current facts for its source authority, generation, and sequence. A newer complete cut replaces the prior cut from that endpoint atomically. If a source restarts, its new generation identifies its fresh sequence. Expired, missing, unmapped, or conflicting facts disappear from the next cut; an empty `facts` list is a valid complete cut. This is needed because Fact v1 identity includes the edge destination, so a new destination alone does not overwrite an older edge under the same key.

### Keep source and decision ledgers separate

The endpoint reads existing source state and returns an identity snapshot. It performs no scheduling or placement action, so there is no decision-ledger event to write. Tests verify no planner dispatch occurs on identity reads.

## Risks and Trade-offs

- Existing nodes remain unresolved until operators set both explicit identity variables; old behavior keeps running.
- Principal files need an optional Benchday host mapping for each workload worker. The mapping is protected by the same file permissions and reload rules as worker credentials.
- A 60-second Fact TTL requires the source reader to refresh at a cadence comfortably below 60 seconds; a missed refresh degrades to unknown.
- The source endpoint is authenticated, but facts still express the source's assertion. Benchday must bind each endpoint credential to its configured authority and allowed identity namespace.

## Migration Plan

1. Land this source proposal and add tests before changing producer code.
2. Add explicit node identity fields to the capability response and keep the change additive.
3. Add authenticated fleet and workload identity snapshot endpoints. Keep existing routes and clients unchanged.
4. Configure one test Harmony node and one test workload worker, then verify missing configuration omits facts.
5. Integrate the endpoints in Benchday, test expiry and reassignment, then update production source configuration only through its normal deployment workflow.

No data migration is required; existing inferred values are not promoted into facts.

## Open Questions

- None blocking.
