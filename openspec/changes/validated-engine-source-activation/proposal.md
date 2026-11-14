## Why
The engine adapter correctly refuses source/pin mismatches, but operators have no reviewed command to update the source selection and pin together. This blocks the merged Flash Next cessation runtime. `_plans/harmony-engine-units.md` describes old setup-based re-pinning; its manual release workflow is stale.

## What Changes
Add an operator-only plan/apply command for a committed Strata source and verified native artifacts. It preserves other units, acquires the existing exclusive-device lease, checks the original declaration digest immediately before replacement, journals transitions and rolls back a failed restart. It does not choose residency or load a model.

## Capabilities
### Modified Capabilities
- `harmony-engine-units`: validated, leased, atomic source activation.

## Impact
Python operator tooling, isolated filesystem/HTTP controls, operator runbook. No caller grammar, model-quality acceptance or authority/worker deployment change.
