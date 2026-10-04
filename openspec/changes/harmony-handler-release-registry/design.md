## Context

See proposal.md for motivation and the new `workload-handler-releases` contract in `specs/workload-handler-releases/spec.md`. The authority currently reads its handler names and policy from startup configuration; workers read executable descriptors from their configuration at construction. The authority already owns durable jobs, attempts, worker reports, fences, object CAS and the authenticated `/v1/workloads/` surface. Worker execution is out of process and the worker has a durable attempt journal.

## Goals / Non-Goals

**Goals:** keep authorization and execution policy under operator control; let compatible command-handler releases change without restarting authority or worker sessions; make every accepted release-aware job reproducible through cleanup and recovery; keep release storage, metadata, histories and reconciliation bounded.

**Non-Goals:** hot-reload Python modules in the authority or worker; change admission resources, lease semantics, compiler authorization, host policy, or supervision backend; allow submitters to upload executable code or choose paths, interpreters, credentials, or network endpoints; transparently replace a release on an accepted job.

## Decisions

### State ownership and identifiers

| State | Durable owner | Identifier crossing the process boundary |
|---|---|---|
| Authorized handler IDs from startup policy, immutable manifests, package archive references, defaults, generations, activation receipts, accepted intent and job release identity | Authority SQLite plus its bounded content store | `handler_id`, `release_digest`, `archive_digest`, monotonically increasing `registry_generation`, `job_id` |
| Installed package bytes, validated local registry pointer, and in-progress attempt descriptor | Worker state directory and immutable package root | `release_digest`, `registry_generation`, `attempt_id`, `fence`, `worker`, `boot` |
| Product request and result schema | Submitting caller and handler contract | `payload_schema`, `result_schema`, `execution_contract` |
| Product decision and gate evidence | Benchday consumer | Authority-returned job/attempt/release identities, checked before gate acceptance |

The authority owns global defaults and acceptance-time resolution. A worker owns only its verified local cache and effective-generation pointer. The journal carries the accepted release identity so a worker never asks the current default to interpret an existing attempt. Result identity repeats the authority's accepted values and is checked against the stored attempt fence before completion.

### 1. Canonical package identity, separate from transport bytes

Use a versioned `harmony-handler-package.v1` manifest. It identifies one logical handler, human release label, execution-contract major, payload/result schema IDs, supported OS/architecture and installed backend, a configured runtime ID, one relative entry point, a bounded fixed argument list, declared product and infrastructure outputs, infrastructure exit codes, and a complete sorted file inventory containing each path, normalized mode, byte count and SHA-256. The runtime ID resolves through operator-installed worker configuration; the package cannot supply the interpreter or executable path. Existing host, compilation, signing, network, and resource controls remain authoritative and are never fields that a package can grant.

`release_digest` is SHA-256 over the canonical UTF-8 JSON encoding of the manifest body without its digest field: object keys sort lexicographically, strings are NFC, integers only, arrays retain declared order, and there is no insignificant whitespace. A fixture shared by Benchday and Livestack pins the exact bytes and digest. A separately computed `archive_digest` identifies the transport archive in the existing content store. The worker verifies the archive digest, then the canonical manifest digest and every listed file before installation. Archive paths must be unique normalized POSIX relatives. Links, special files, traversal, undeclared files, executable mode outside the normalized allowlist, and files exceeding declared bounds are refused. An archive may not run code during validation.

The worker invokes only the configured runtime for the manifest's runtime ID with the installed relative entry point and its fixed argument list. Handler outputs and infrastructure exit codes come from the immutable manifest. The payload remains opaque to Harmony. A release never supplies policy or a transport endpoint.

### 2. Authority registry and live operator API

Add bounded tables to the existing workload SQLite database for release manifests and archive references, the current desired generation, retained generation snapshots, activation receipts, and an event ledger. Handler IDs remain rooted in startup `handlers`, `handler_release_policy.handlers`, and existing principal allowlists, so a package can update code only for a logical handler already authorized by the installed core. Migrations are additive and legacy name-only handlers continue to come from startup configuration. Operator endpoints reuse the existing authenticated admin principal; callers can submit only handler IDs in their existing principal allowlist, while workers can report inventory and fetch only packages the authority has offered to their authorized worker identity.

Package archive bytes enter through the existing bounded authenticated CAS transfer path. A small metadata request stages the manifest and archive digest only after the authority verifies the complete archive and manifest. Staging is idempotent by `(handler_id, release_digest)` and checks the startup operator policy before storing metadata. Activation is one SQLite transaction guarded by `expected_generation`; it verifies the target release is staged and matches the existing handler/runtime/backend policy, then commits a new immutable generation and a durable receipt. A repeated activation request ID returns that receipt. A stale expected generation returns a named conflict. Rollback creates another generation selecting a retained older digest and cannot edit accepted jobs. The authority retains the current and previous registry snapshots, at most 16 receipts, and one prior release per handler for rollback.

Receipts distinguish authority desired state, per-worker effective state, and the last generation observed by the authority. The receipt ring is capped at 16. Every response and refusal is logged with its handler ID, digest, generation, actor and named outcome; secrets and package contents never enter logs. Runtime handler validation never silently turns an error into an empty inventory.

### 3. Resolve intent transactionally at acceptance

For a new release-aware request, the handler release selection is either `{selection: "default"}` or `{selection: "exact", release_digest}`. The HTTP layer authorizes the logical handler with the submitter's existing principal allowlist before acceptance. Inside the existing `BEGIN IMMEDIATE` transaction, the store first checks the idempotency key against the normalized submitted intent, including the selection form and exact digest when present. For a new key it resolves the current default or validates the selected digest and persists both `handler_release_intent` and the resolved release descriptor, including execution contract and payload/result schema IDs, with the job. The existing request hash therefore remains the submitted intent hash; legacy request hashes keep their present meaning.

This ordering means a retried default-based request returns its original job even after activation, while a request that changes from default to an explicit digest conflicts. The job row pins the immutable manifest digest and contract tuple; each attempt row and worker journal carry that same tuple plus the existing attempt fence. A worker cannot choose a default.

### 4. Live worker activation through the existing control exchange

The worker reports its verified inventory and effective generation in its existing registration report. The authority returns the bounded desired registry snapshot in that response. The worker checks it on its existing report loop, including status reports during an active attempt; it does not create one watcher or authority poll per handler. It downloads missing archives only from the fixed authenticated authority object route, verifies and extracts to a private temporary directory, fsyncs, then atomically installs immutable package directories and commits the local effective-generation pointer.

During an active attempt, a status report can fetch and preflight the next generation while the supervised child continues and the lease keeper continues heartbeats. The worker finishes registration and sync before it makes another claim. The assignment descriptor is already pinned before launch, so installing B cannot change an A process, completion classification, artifact selection, cleanup, or journal recovery. If synchronization fails, the prior effective generation remains available, the worker reports a named error and advertises only verified bytes. It never claims a job until its exact release is locally present.

### 5. Exact inventory placement and result binding

Worker reports contain a capped set of `{handler_id, release_digest, execution_contract, payload_schema, result_schema}` entries. Registration stores that inventory with the worker heartbeat and generation. Claim queries require an exact digest and compatible tuple for release-aware jobs; stale workers, legacy inventories and missing releases cannot match. Wait status includes the required handler and digest. The existing SQL claim transaction writes the selected digest and generation to the attempt row, so requested and selected identity are inspectable with the job and attempt. The query must be set-based; reference counts and inventory checks must not open one SQL connection per job or worker.

Assignments carry the full immutable descriptor needed for execution, not a mutable handler-name lookup. The worker journal stores that descriptor before launch. The same object drives entrypoint, environment, exit classification, output declarations, upload and cleanup. Completion carries the assigned identity; the authority compares it to the persisted job and attempt inside the completion transaction and returns a server-bound result envelope. Older jobs keep the existing legacy completion shape and are labeled legacy in status.

### 6. Explicit package, metadata, history, and reference bounds

Enforced limits are: 64 handler IDs; 4 catalogued releases per handler; 2 GiB per package; 16 GiB across authority package archives and 16 GiB across worker installed package trees; 4 MiB total manifest metadata; 10,000 files per package; 16 unreferenced staged candidates; 16 retained activation receipts; 1,024 authority release events; and a configured retention window of at least 24 hours. A worker package root also caps at 256 installed packages and 260 total entries, including at most three transient download, extraction, and pointer files. Startup removes interrupted transient files; excess entries refuse synchronization. Unknown sizes, invalid policy, and an unset deletion window cannot be interpreted as zero. The existing jobs, attempts, worker, record, object and workspace limits continue to apply.

The authority reference query returns a set covering retained registry defaults, the latest previous release per handler, accepted nonterminal jobs, running/cleanup attempts, and fresh worker effective defaults. It is one bounded SQL round trip per collection batch, with one query and at most 32 bounded deletion/event writes. Worker local collection protects its effective defaults, active journal descriptor, and digests from the authority snapshot; incomplete references or an unset retention window preserve packages and produce an explicit receipt. At capacity, staging refuses instead of evicting a referenced release.

### Alternatives considered

- Import handler modules in the worker process: rejected because an import or global-state failure would become a core-process failure and accepted work could change underneath an attempt.
- Change the worker's config path and restart it: rejected because it recreates the reported outage and boot/fence churn.
- Resolve the default at claim or launch: rejected because queued work and retries would silently change behavior during rollout.
- Trust only the semantic version or mutable package path: rejected because those do not identify executable bytes or completion policy.
- Let the package declare capabilities: rejected because code release authority must not become signing, compilation, host, or network-policy authority.

## Risks / Trade-offs

- Workers may need to download up to the configured package cap while a job is running → fetch and verify outside the attempt supervisor, keep the heartbeat path live, and retain the previous effective generation until atomic commit.
- A release may require a host runtime not yet installed → runtime IDs are operator-installed and compatibility failures remain named waits; new core contract or runtime support requires the guarded core rollout.
- Queued jobs can pin old packages and fill the byte budget → bounded job lifetime, visible capacity refusal, and reference-aware collection.
- A crash can occur between package install and pointer commit → immutable package directories are harmless until referenced; the fsynced generation pointer selects one complete registry on recovery.
- Authority and worker package versions may disagree → canonical cross-repository fixtures, exact digests, and result identity checks reject disagreement.

## Migration Plan

1. Ship the paired Benchday and Livestack contract fixtures and schemas. Add the authority package catalog and release-aware protocol while keeping legacy configuration and callers explicit.
2. Upgrade the authority and eligible workers once using the existing guarded rollout. Install the registry state directory and configured runtimes; do not claim a worker has a release until its verified report names it.
3. Register and install Benchday E2E packages, then exercise A/B activation under a live attempt, queue pinning, retry, rollback, recovery and result identity in isolated real authority/worker tests.
4. Migrate compatible build/release producers and consumers to exact identity, enumerate worker compatibility from authority reports, and preserve name-only workers as legacy.
5. Rollback changes defaults to retained releases. Disable release-aware placement only after no accepted release-aware job remains; refuse a core downgrade while such jobs or journals are pinned.

## Verification

Use real temporary HTTP authority, SQLite, content store, and supervised worker processes with two tiny packages that produce distinct output names and exit classifications. Verify busy activation leaves the worker PID, boot, active lease and A result intact while new work selects B; crash/recover on both sides of authority and worker registry commits; test lost acknowledgments, generation conflict, changed idempotency intent, mixed inventories, result forgery, path/digest tampering, capacity refusal, retention exemptions and unset-window refusal. The fixed TLA+ model and an intentionally broken model are checked by the paired Benchday change. Every gate assertion runs only through Benchday's isolated test train.
