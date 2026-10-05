## Why

The handler release registry refuses to stage a fifth release for any one handler, even when storage is nearly empty. A development burst (several handler generations in one day while iterating) therefore blocks a legitimate release until a 24-hour timer frees a slot, and the CLI has no retire command. A count per handler is the wrong bound: the resources worth protecting are bytes, catalogue metadata, worker disk and entries, and the registry already bounds all of them directly. The per-handler count only adds friction.

## What Changes

- Remove the per-handler release-count refusal (`handler_release_count_capacity`). Storage is the bound.
- Keep every storage bound (handler ids, archive bytes, manifest metadata, unreferenced candidates) and add one total release count (256) that matches a worker's installed-package cap and the status listing.
- When a bound would be exceeded, evict the oldest unreferenced releases that are past an explicit minimum age, all-or-nothing, before refusing. Refuse with the existing named outcome only when the whole deficit cannot be covered.
- Add `burst_min_age_seconds` to the handler release policy in the authority config file: schema-validated, extra keys refused, floor one hour, at most the retention window. Unset disables eviction (fail closed).
- Surface the policy (`burst_min_age_seconds`, limits, no per-handler count) in registry status; record every eviction and refusal as a bounded event.

## Capabilities

### New Capabilities

### Modified Capabilities
- `workload-handler-releases`: the bounded-staging requirement changes from a per-handler count to storage bounds with capacity-driven, reference-safe eviction.

## Impact

- `node-py/livestack_node/workloads/handler_registry.py`: constants, policy validation, a shared reference-evidence query, `_make_room`, `stage`, `collect`, `status`.
- `node-py/livestack_node/workloads/config.py`: `HandlerReleasePolicy` pydantic model (extra=forbid).
- `node-py/tests/test_workload_handler_registry_burst.py` (new) and the removal of the old four-release test.
- `_plans/durable-workloads.md`: hard bounds text.
- Operations: the live authority gets `burst_min_age_seconds` in its config and a restart; rollback restores the previous release directory and config.
