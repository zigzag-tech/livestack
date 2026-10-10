## Why

The host broker's `/status` endpoint shows peer health and only the most recent eviction, so a fleet monitor cannot report recent serving outcomes, unit queue depth, or latency. A bounded in-memory view lets consumers observe current pressure and recent results without turning telemetry into another durable store.

## What Changes

- Add per-unit-kind rolling 60-minute bucket rings for admitted, completed, failed, and evicted outcomes.
- Expose a per-unit queue-depth gauge when the unit reports queue state, and p50/p95 job latency from a bounded reservoir of at most 256 reported samples per unit.
- Return the telemetry under `/status.counters`; preserve unknown values as absent rather than inventing zeros.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `harmony-engine-units`: define bounded host status counters, queue gauges, and latency summaries for unit serving outcomes.

## Impact

- `node-py/livestack_node/hostbroker.py` and `hostd.py` own the in-memory counters and status projection.
- `node-py/tests/` covers bucket expiry, lifecycle outcomes, queue-depth projection, reservoir bounds and percentiles, and the `/status.counters` response.
- This realizes the host-broker observability surface in `_plans/fleet-broker.md`; its §0 gap inventory predates the current implementation and does not specify bounded per-unit outcome telemetry.
