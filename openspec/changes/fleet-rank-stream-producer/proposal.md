## Why

Benchday consumes Livestack fleet rankings through the authenticated `harmony.fleet-rank/1` stream, but Livestack's existing native producer still emits the earlier pair-keyed body. That mismatch prevents the stream from carrying the Hub's per-vantage snapshots and explicit removal state.

## What Changes

- Align the Livestack producer with the current bounded `harmony.fleet-rank/1` contract: key each snapshot by `(kind, region, vantage)`, mint a stable content-based decision id, and emit explicit present or removed bodies.
- Keep registration and publication on the existing authenticated daemon-ingress path; preserve named failures, source bounds, and the existing ranker behavior.
- Update the producer design record where its original one-ranking-per-`(kind, region)` assumption is stale.

## Capabilities

### New Capabilities

- `fleet-ranking-streams`: Livestack publishes bounded, authenticated, per-vantage fleet-ranking snapshots and removals.

### Modified Capabilities

- None.

## Impact

This realizes `_plans/fleet-broker.md` §8.1's authenticated push decision. That section is stale about the source key: it describes one ranking per `(kind, region)` and predates the Hub contract's `vantage` key, strict body schema, and explicit removal state. The change affects `node-py/livestack_node/streams/fleet_rank_stream.py`, `streams/runtime.py`, and focused producer tests. It adds no producer-side durable state or placement decision; the content-derived `decision_id` is carried into the Hub manifest and its existing route ledger correlation.
