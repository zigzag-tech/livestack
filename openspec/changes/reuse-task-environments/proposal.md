## Why

Successive agent build/test jobs discard preparation and compilation state even when the task and toolchain are unchanged. Six successful Benchday Flutter test jobs on 2026-10-03 each compiled 79 Rust crates in 31.7–33.3 seconds; those builds consumed 194.5 seconds of their combined 616.6 seconds from acceptance to completion. Preserve useful disk state while acquiring and releasing CPU/RAM for every execution.

## What Changes

- Extend the existing durable workload submission API with version 3 and an optional owner-scoped reusable environment reference. Keep versions 1/2 and their idempotency bytes unchanged.
- Resolve a caller-chosen stable task key into an opaque environment handle on ordinary job submission; later requests may use either identity. Each execution remains a separately admitted, fenced job.
- Prefer compatible existing environments during placement, with a bounded affinity wait and a cold reconstruction path on another eligible host.
- Preserve bounded source materialization, dependency caches and compilation outputs. Stop all owned processes and release execution resources after every command, retaining disk only.
- Add authenticated capability discovery, environment inspection, typed reuse receipts, SDK support and existing workload CLI flags. The caller does not create a second scheduler or manage host directories.
- Define agent instructions and a task/handoff contract: keep the environment identity across source updates, resume the accepted job on transport uncertainty, and report rebuilding separately from workload failure.
- Limit environment-enabled handlers to development builds/checks/tests and explicitly selected task E2E. Full/coalesced E2E and publishing/release handlers reject environment requests and retain their existing unified orchestration.

## Capabilities

### New Capabilities

- `workload-environments`: request/SDK/CLI contract, task identity, compatible best-effort reuse, fresh admission, fencing, storage bounds and attributable outcomes.

### Modified Capabilities

None. Existing fleet provisioning and supervision requirements continue to apply; persistent environments do not pin or rent a machine.

## Impact

`node-py/livestack_node/workloads/{model,store,placement,worker,client,cli}.py`, the pure fleet scheduler, worker execution backends, bounded workspace provisioning and decision records. Companion consumer proposal: Benchday `openspec/changes/reuse-task-environments/` (including the selected infrastructure SDK dependency). Existing compilation authorization remains mandatory.

This realises the deferred image/build-cache work in `_plans/durable-workloads.md` ("Worker source cache"). Its source-object cache remains useful, but its attempt-only scratch lifecycle cannot describe retained task environments. Draws from `_plans/fleet-broker.md` and `_plans/decision-ledger.md`; no design record is claimed shipped by this proposal.

## Non-goals

Holding compute between jobs, live-process/VM suspension, arbitrary remote shells, keeping test databases alive, full/coalesced E2E environment reuse, publishing/release environment reuse, changes to either unified orchestrator, paid provisioning caused solely by stored environments, guaranteed cache survival, shared mutable caches between owners, changing inference residency, or claiming the measured cold/warm differences as delivered savings. Operational rollout and archive require implementation evidence and completion of this change's tasks.
