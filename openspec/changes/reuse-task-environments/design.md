## Context

See proposal.md for measurements and scope. Current workload schemas 1/2 reject unknown top-level fields; `key` identifies one idempotent submission. `locality_host` already supplies a placement preference, but workers remove attempt workspaces on completion. The source archive cache does not preserve compiler targets. Compilation authorization and measured host admission remain independently authoritative.

## Goals / Non-Goals

**Goals:** Make repeated work use a persistent logical environment through the existing job API; release execution resources between jobs; give agents concrete submission, inspection and handoff interfaces; preserve input/result attribution and ordinary queue fairness.

**Non-Goals:** See proposal.md. Stored environments are reconstructible acceleration state, not the authoritative copy of user edits or artifacts. Version 1 introduces no arbitrary shell execution, process snapshots or CPU/RAM grace reservation.

## Decisions

### 1. Extend ordinary submission; negotiate explicitly

Keep `POST /v1/workloads/jobs`. Schema 3 retains the current fields and adds an optional `environment`:

```json
{
  "version": 3,
  "key": "test-terminal-fracture-run-002",
  "handler": "benchday.compilation.flutter-check.v1",
  "input_digest": "<SHA-256 of captured source>",
  "need": {"cpu": 4, "memory_bytes": 4294967296},
  "selector": {"os": "linux"},
  "estimate_seconds": 5400,
  "environment": {"key": "benchday-terminal-fracture-linux", "reuse": "prefer"},
  "payload": {
    "version": 1,
    "mode": "test",
    "arguments": ["test/widgets/terminal_fracture_reveal_test.dart"],
    "input_digest": "<same captured source digest>",
    "source_manifest_digest": "<SHA-256 of source manifest>"
  }
}
```

`environment` has exactly one of `key` or `handle`, plus `reuse: "prefer"`; unknown fields/modes refuse. Keys use the existing bounded name grammar; handles are opaque authority-issued names, never paths or bearer credentials. Omit `environment` for an ordinary disposable job. Schema 3 can carry the schema-2 `input_objects` contract, independently of environment selection. Environment identity never incorporates the entire source digest: source changes are the purpose of reuse.

Add authenticated `GET /v1/workloads/capabilities`, returning supported submission versions and `environments: {version: 1, handlers: [...]}` for this principal (bounded by the existing handler allowlist). This is read-only and reserves nothing. Unknown/absent capability never means supported. The SDK checks once per client session, caches at most 60 seconds, and invalidates on a version refusal. An explicit environment request fails `environment_unsupported` before source upload when unsupported; a caller may explicitly opt out and submit a legacy job. No silent field stripping. Versions 1/2 retain their canonical request bytes and response compatibility.

The first normal submit resolves/creates the logical environment and returns its handle; a separate create request is unnecessary. Submission and resolution are atomic with the job's idempotency admission. Repeating a lost submission reply returns that job and handle. Reusing its job key with a different input, environment reference or payload conflicts. Switching between key and handle is a new submission representation and must not rewrite the stored request identity.

### 2. State ownership and identity boundaries

| State | Owner | Identity crossing the boundary |
|---|---|---|
| Logical environments and task-key index | Workload authority, existing durable database | `(principal, delegated owner, environment key)` → handle |
| Accepted jobs, environment bindings, writer generation and attempt fences | Workload authority | job ID, handle, generation, attempt ID, boot/fence |
| Materialized source, compiler outputs, dependency caches and quota counters | Supervised worker on its dedicated bounded filesystem | handle + generation + compatibility digest; no caller paths |
| Captured source and important result artifacts | Existing immutable CAS | job input digest and verified output digests |
| Optional task binding on a developer machine | Consumer wrapper | authority identity, owner scope, task key, environment handle, last job ID |
| Placement/reuse/cleanup measurements | Existing bounded decision/result stores | decision ID joined to job, attempt, handle and generation |

Resolve `labels.owner` under the existing delegation authorization; without it, the principal is the owner. Identical keys in different ownership scopes are different environments. Handles are authorized on every operation. Foreign/unknown handles return the same scoped not-found response. Resolved handles appear beside the immutable request in job metadata, not by mutating `spec` or its request hash.

An authenticated `GET /v1/workloads/environments/<handle>` returns state, logical revision, current/last producing job, bounded replica summaries, disk use, expiry, and last reuse outcome. No host filesystem paths, secrets or source text. The existing `get` job API remains sufficient for observation. After metadata expiry an old handle returns scoped not-found; submitting the original task key creates a new handle and reports creation. Cold recreation of an existing handle increments its generation and reports rebuilding.

### 3. Fresh admission and exclusive environment ownership

Lifecycle: `empty → preparing → running → cleanup → parked`; failures may leave `rebuild_required`, eviction leaves `evicted`. Every execution enters the ordinary job queue. Resolving a handle reserves no CPU/RAM and grants no priority. Placement atomically acquires the normal physical-host resource claim and one authorized writer generation for the environment. An environment-busy job remains queued without holding execution resources or blocking unrelated jobs. Job deadline and per-owner concurrency limits remain unchanged.

The worker additionally locks the concrete replica across all worker identities on that physical host. Another worker on the same host can reuse it after cleanup; worker identity is not the cache identity. Readers/inspection do not take a writer claim.

Park only after owned processes, containers, compiler servers, mounts and sockets have stopped or detached, verified by the existing supervisor. Preserve the approved disk directories, not the attempt's runtime. Release CPU/RAM only through existing cleanup confirmation. Parked environments have no heartbeat, running-count charge or compute reservation. A cleanup failure retains the appropriate execution claim and reports `environment_cleanup_pending`.

Cancellation, expiry and worker/authority restart fence results and environment commits. A stale writer cannot publish a new logical revision. If an unreachable host could still be writing its private replica, a replacement may use a new fenced generation on another host under the existing retry/cleanup rules; it never reuses that unconfirmed replica. The old host must reconcile/stop before serving it again. Durable CAS inputs allow reconstruction without trusting interrupted workspace state.

### 4. Compatibility and complete source updates

Only installed handlers explicitly enrolled for environment support can request it. Enrollment declares execution purpose `development` or `task_e2e`; full/coalesced E2E and publishing/release handlers are excluded and return `environment_scope_forbidden` when referenced. The purpose is installed operator policy, never caller-selected permission. A task-E2E handler requires a nonempty bounded explicit lane/check selection and rejects `full`, wildcards resolving to all checks, or expansion/coalescing into a full run before execution. An ordinary authorized full/release job without an environment remains usable through its existing unified orchestrator. Their worker-owned profile declares retained paths, source/build layout, preparation checks and runtime-reset behavior. Requests cannot name shell commands, mount paths, arbitrary cache directories, toolchains to install, or compilation privileges.

Compatibility covers handler cache-contract version, OS/architecture/ABI, actual compiler/SDK identities, build mode/options that affect artifacts, dependency resolution inputs and native build recipe. Keep separately addressable cache components: changing a lockfile invalidates installed dependencies while verified downloaded objects may survive; a compiler/ABI change invalidates incompatible targets. Ordinary application-source changes trigger the build tool's incremental rebuild. Handler release changes preserve caches only when their declared cache contract remains compatible. Missing compatibility evidence causes a named rebuild/refusal, never a claimed hit.

Each job still receives a verified immutable captured source. Reconcile the private environment source mirror against its complete manifest, including deletions, symlinks, modes and workspace dependencies. Preserve verified unchanged files and their useful timestamps; remove obsolete source and unapproved generated inputs. Only explicitly declared, compatible cache/output paths survive. Check source integrity before/after handler work and stamp outputs against the current source/build recipe. A no-op build remains a verified execution, not permission to accept yesterday's binaries.

Preserve the environment's stable internal path where possible. Keep credentials in transient attempt-owned files; exclude credentials, logs, databases, ports, test fixtures, result receipts and user-generated authoritative data from retained cache paths. Failed source synchronization or interrupted preparation marks the replica `rebuild_required`; it cannot be reused until a full verified reconstruction succeeds. A product test failure can retain successfully verified compiler/dependency state and remains a terminal product failure.

### 5. Best-effort placement uses bounded affinity

Filter hard policy, platform, handler support, compatibility, fresh capacity, owner quota and avoid-worker rules before applying affinity. Prefer a compatible available replica. Where both preparation/runtime and queue estimates have measured bases, compare estimated finish times; unknown ETA remains unknown.

To prevent an old busy host trapping an otherwise runnable job, affinity may defer an eligible cold placement for at most 15 seconds from the first observed eligible alternative (operator may lower to zero). The timestamp is durable and never resets on heartbeats/replanning/retries. At its bound admit the eligible alternative through ordinary policy. Hard constraints and lack of capacity can still produce a genuine resource wait. With unknown estimates, use this fixed cap rather than inventing a savings estimate. Caller-supplied `locality_host` remains a preference; it cannot override a hard filter or this cap.

Relocation initially reconstructs from CAS and warms local caches; copying the previous host's mutable build directory is unnecessary. Allow at most two replicas per logical environment, all charged to storage caps; sweep inactive superseded generations. Stored environments do not block drain/deprovision, trigger paid provisioning, or confer queue priority. Any reconstruction is visible, including after eviction.

### 6. Bounds, enforcement and failure visibility

Initial operator defaults/ceilings (operators may lower them):

| Item | Bound | Enforcer |
|---|---|---|
| Logical registry, including tombstones | 1,024 global / 64 per owner; 4 KiB per row | Atomic authority admission + expiry sweep |
| Replica summaries | 2 per environment / 64 per physical host | Authority registration validation and batched reconciliation |
| Environment storage, source plus outputs | 32 GiB per replica / 128 GiB per owner per host | Kernel filesystem/project quota or separately bounded volume + worker admission |
| Aggregate environment storage | At most 256 GiB per host AND provisioned workspace free/reserved budget, whichever is smaller | Shared physical-host disk ledger; account attempt scratch/input caches too |
| Retention | Idle 7 days, absolute generation age 30 days | Worker/authority sweep every 60 seconds; unset/zero window disables deletion, names unavailability and refuses retention admission |
| Sweep/reconciliation work | At most 64 rows/replicas, 5 seconds per pass | Batched bounded queries; unfinished deletion stays charged |
| Environment receipt | 16 KiB, one current receipt per attempt plus existing bounded job retention | Existing result/ledger bounds |
| Environment inspect response | 16 KiB, 5-second operation bound | API/SDK validation |
| Local agent binding map | 64 tasks / 64 KiB / 30-day idle expiry | Consumer atomic file writer and explicit expiry pass |

Kernel bounds must apply to child writes, including native Docker/host backends. A periodic `du` alone is insufficient. Active generations are never evicted. Queued jobs do not pin cache bytes: they can recreate their environment from retained job inputs. Retention exemptions apply to source/artifact CAS under existing policy; an exempt payload is never stored solely in disposable environment state. An inability to enforce quota disables environment support on that worker. Deletion failures remain counted and observable, never block unrelated healthy workers. Disk pressure can evict eligible inactive caches or give a named storage refusal.

### 7. Agent SDK, CLI and receipts

Extend `WorkloadClient.submit` and its typed request/response contract; add `capabilities()` and `get_environment(handle)`. Validate environment options before upload. Existing workload CLI remains the interface:

```bash
python -m livestack_node.workloads.cli --config <private-config> submit request.json --environment-key <task-key>
python -m livestack_node.workloads.cli --config <private-config> submit request.json --environment-handle <handle>
python -m livestack_node.workloads.cli --config <private-config> environment get <handle>
python -m livestack_node.workloads.cli --config <private-config> get <job-id>
```

Flags select one reference, upgrade the envelope to schema 3 explicitly, and conflict with a different JSON reference. `--no-environment` explicitly selects a disposable request and conflicts with any reference. CLI submission validates the caller's existing job key rather than silently rewriting it; the consumer is responsible for changing job identity when inputs/options change. Commands keep machine-readable JSON on stdout, diagnostics on stderr, nonzero exit for refusal/transport failure, and pending jobs clearly pending. `get` observes; it never resubmits. No generic `exec <shell string>` command is introduced.

The job response contains owner-authorized `environment: {handle, requested, generation?, state, ...}` separate from its request. Terminal attempt receipts name actual host/boot/fence/input and compatibility digests with `reuse.outcome: created|reused|rebuilt|relocated`, fixed `reason_code`, previous producing job when known, bytes retained, and bounded phase timings. Reuse failure and product failure are different fields. Emit timings for queue, transfer, materialization, dependencies, compilation, test execution and cleanup where measured; unsupported measurements are null with a reason, never zero. Emit logical-environment busy/affinity/resource waiting reasons at the authority.

Record each placement, generation transition, invalidation, eviction and cleanup failure in the bounded decision store, joinable by IDs above. Candidate lookup/claims/sweeps are batched independently of entity count; no database query per environment or worker. The pure scheduler consumes the snapshot and returns choices without side effects. Failed decision recording carries `observability_degraded` through status.

Agent guidance belongs in upstream API/CLI examples and each consumer's canonical instruction file. The exact Benchday instruction text and handoff record are in its companion design. There is no Livestack `AGENTS.md`/`CLAUDE.md` in the inspected tree; document the upstream interface in `HARMONY.md` and the workload CLI runbook rather than inventing a second competing policy file.

## Risks / Trade-offs

- Stale build outputs or partial source synchronization → verify the full manifest, segregate cache components, run normal build freshness checks and rebuild on unknown state.
- Preferred host unavailable or busy → bounded affinity, cold relocation and explicit outcome; disk state is disposable.
- Retained Docker storage escapes the environment quota → environment enrollment requires a real filesystem-bound positive/overflow control for every enabled backend.
- Concurrent agents sharing a handle serialize → use separate task keys for independent branches; one handle intentionally means one authorized writer.
- Rollout mistakes expose unsupported fields → capability negotiation, schema-version rejection and no silent downgrade.
- Reported 31.5% native-build share becomes a marketing speedup claim → retain measurements as baseline; measure repeat executions and queue separately before any savings claim.

## Migration Plan

1. Implement/verify authority schema, registry and SDK/CLI against disposable HTTP/database services; preserve legacy identities.
2. Implement one bounded Linux worker environment profile and real child cleanup/quota controls; enroll only supported handlers/backends. Add formal or real state-machine checks for exclusive writer, stale generation and no parked compute claim.
3. Pin the new runtime in the selected consumer SDK. Deploy authority then workers then wrappers after drain/idleness checks; authenticate capability readback.
4. Exercise cold/repeat/edit/deletion/invalidation/eviction/relocation/cancellation sequences on admitted disposable fixtures. Alternate cold/warm runs on the same host/toolchain; report phase and core-second data, with no claimed queue savings from unmeasured intervals.
5. Complete the Benchday changed-assertion train gate through its existing unified orchestration and agent instruction/CLI acceptance. Verify full E2E and publishing environment requests refuse; neither orchestrator gains retained task state. Rollback disables new environment submissions and drains active generations; it preserves ordinary legacy admission and artifact verification. Inactive disk is reclaimed only under recorded bounds.
6. Update the historical runbooks and bounds inventory, publish the minimum affected runtime/consumer components, and archive both changes only after their own implementation, gate and rollout tasks complete.
