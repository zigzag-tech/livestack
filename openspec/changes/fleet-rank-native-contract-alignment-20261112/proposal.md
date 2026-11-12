## Why

The current native producer on origin/main publishes harmony.fleet-rank/1 with
triple subjects and a body that lacks region and decision_id. The Benchday
catalog/consumer work declares harmony.fleet.rank/1 with pair subjects. Those
wires cannot meet. This change completes the native producer side of the
owner's push path in _plans/fleet-broker.md section 8; its fleet_rank ad-hoc
message and daemon polling detail are stale under everything-is-a-stream.

## What Changes

- **BREAKING:** publish harmony.fleet.rank/1 with (kind, region) subject keys,
  one configured vantage per pair, and the full declared bounded body.
- Preserve the actual ASR attribute and measured-metric preference vocabulary.
- Bound active configured pairs at 32, refuse conflicting duplicate vantages
  by name, and preserve the first 32 on overflow.
- Derive stable, content-sensitive decision IDs that distinguish different
  decisions within a second.
- Publish no-target absence separately from computation/validation failures.
- Keep authenticated native ingress and named authority refusal; add no HTTP
  fallback, credential surface, or production activation step.

## Capabilities

### New Capabilities

- `fleet-ranking-streams`: bounded realm-authoritative native ranking publication
  consumed by Benchday through its stream fabric.

### Modified Capabilities

None.

## Impact

Native stream projection and runtime, focused source/AF_UNIX tests, and the
Benchday source-pinned fixture handoff. The ranker and eligibility policy stay
at their existing boundaries. The dependent catalog and Hub consumer are in
Benchday commits b480f0a7d2 and 75e1630417, pending typed branch verdicts.
