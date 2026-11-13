## Why
Benchday E9 needs an actual authenticated turn → job → attempt/worker → explanation path. The fixed-owner job reader currently rejects source_turn_id and the authority does not store trusted source identity. Labels and fabricated protocol replies cannot establish that path. Owner approval: 2026-11-13 22:58 Asia/Shanghai, including access controls, retention bounds and negative tests.

## What Changes
Add an optional closed, byte-bounded provenance envelope at submission, guarded by explicit principal permission. Stamp its submitting principal/effective owner at the authority. Extend the existing fixed-owner cursor projection with an indexed exact-turn filter and authority-derived explanation. Clear provenance at a bounded age even when a job is retained. Preserve old submissions and readers.

## Capabilities
### New Capabilities
- workload-turn-provenance: authenticated source submission, bounded owner-scoped reads and retention.

## Impact
Realises the missing observational source contract in `_plans/durable-workloads.md`; that plan currently describes owner-scoped durable jobs but not exact-turn provenance. Touches workload submission/model/store/projection/configuration and tests; no scheduling, routing or inference choice changes. Deployment grants remain operator owned.

## Non-goals
No prompt/pane/input storage, new planner, worker selection, broad history scan or authority restart by an application agent. An E9 proof must use an actual authority/worker journey, not mocked job rows.
