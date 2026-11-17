# Proposal

## Why

An admitted build can be faster to use on its consumer host even when another
host compiles faster. Current locality bonuses do not represent result size or
delivery time, and operation variants sharing a handler must not share resource
estimates indiscriminately.

## What Changes

- Add bounded resource-profile and result-delivery intent to workload submissions.
- Rank feasible workers using explicit, attributable usable-result estimates.
- Preserve physical-host compilation policy, fresh resource admission and enforced caps.
- Add bounded, owner/attempt/fence/digest-bound same-host result handoff.

## Capabilities

### Modified Capabilities
- `workload-placement`: resource-profile isolation and delivery-aware placement.

### New Capabilities
- `workload-local-artifacts`: verified same-host delivery without a binary round trip.

## Impact

Generic workload schema, worker, placement, artifact acceptance and transfer.
Companion consumer: Benchday change `policy-driven-linux-build-placement`.
The owner approved this cross-repository design before implementation.

## Non-goals

No application-specific handler names, host overrides, second queue, compilation
policy bypass, signing changes or unbounded result retention.
