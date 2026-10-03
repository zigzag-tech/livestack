# Tasks

## 1. Durable request and agent interfaces

- [ ] 1.1 Add schema 3, strict environment key/handle validation and authenticated capabilities; verify real HTTP legacy identity, version refusal and unsupported-before-upload checks. Ledger: bounded capability/refusal outcomes with no secrets.
- [ ] 1.2 Add atomic owner-scoped environment resolution and job binding; verify real durable database/HTTP restart, lost-response replay, changed-request conflict, foreign handle and same-key/different-owner cases. Ledger: creation/resolution joined to accepted job.
- [ ] 1.3 Add environment inspection and bounded SDK responses; verify scoped not-found, retained/expired handle, unknown timing and response byte/deadline refusal. Ledger: no new history for reads; failures reach the caller.
- [ ] 1.4 Extend the existing workload CLI submit/get surface and Python SDK with selectors, capability negotiation and environment inspection; execute documented commands against a disposable authority and verify JSON/stderr/exit behavior and conflicting selectors. Ledger: preserve accepted IDs and no hidden resubmission.

## 2. Placement, ownership and admission

- [ ] 2.1 Add bounded replica reports and batched registry lookup to the scheduler snapshot; verify increasing environment counts do not increase database round trips and unknown compatibility never becomes a hit. Ledger: bounded candidate values and exclusions.
- [ ] 2.2 Atomically combine environment writer generation and ordinary host resource admission; verify concurrent same-handle requests, independent environments and same-host multi-worker reuse with a real authority. Ledger: environment-busy vs resource wait vs admitted.
- [ ] 2.3 Add compatible-host preference and durable capped affinity; verify busy preferred host, available alternative, unknown ETA, restarted authority and policy-excluded old host cases. Ledger: measured/unknown estimate components, affinity start/expiry and actual choice.
- [ ] 2.4 Enforce installed development/task-E2E purpose and bounded proper-subset selection; verify full/coalesced E2E, publishing/release, absent scope and caller-spoofed purpose all refuse, while legacy no-environment orchestration still works. Ledger: environment_scope_forbidden and exact task selection identity.

## 3. Bounded worker environments

- [ ] 3.1 Provision environment storage with per-replica and aggregate kernel bounds on the first supported Linux backend; verify an actual child overflow, owner isolation and physical-host disk accounting. Ledger: disk admission/refusal and retained byte readings.
- [ ] 3.2 Implement complete source mirror reconciliation and handler-owned compatibility/cache components; verify real unchanged/Dart-edit/native-edit/deleted-file/symlink/lockfile/compiler/ABI sequences without stale artifact use. Ledger: source/recipe identities and each reuse/invalidation outcome.
- [ ] 3.3 Separate retained disk from attempt runtime and implement park only after supervised cleanup; verify real descendant, container/socket, cancellation and lease-expiry controls leave no compute claim when parked. Ledger: cleanup pending/confirmed and resources released.
- [ ] 3.4 Reconcile worker/authority restart and stale generation writes, using new private reconstruction when the old replica is uncertain; verify old-result fencing and returning-worker cleanup with real processes. Ledger: fence/generation/rebuild reason.
- [ ] 3.5 Enforce metadata/replica/byte/age bounds and bounded batched sweeps; verify active protection, idle/absolute expiry, unset-window refusal, deletion failure charges, eviction/recreation and host drain/deprovision independence. Ledger: each eviction/refusal/degraded sweep outcome.

## 4. Receipts, consumer acceptance and rollout

- [ ] 4.1 Produce bounded per-attempt environment receipts and measured phase timing; verify known compilation positive controls, unavailable measurements and recording failure propagation. Ledger: join all environment/placement/outcome records by decision/job/attempt/handle/generation.
- [ ] 4.2 Add real integration/state-machine coverage for exclusive authorized writer, no parked compute and stale-generation exclusion; run the glob-discovered relevant checks on an admitted build/test host. Ledger: retain test evidence/failed controls, no production probes presented as tests.
- [ ] 4.3 Update `HARMONY.md`, workload CLI/API examples and `_plans/durable-workloads.md` with the implemented task workflow and full-E2E/publishing exclusion; execute the documented CLI sequence against disposable services. Ledger: no new durable state; examples preserve existing observation/result IDs.
- [ ] 4.4 Roll authority, supported development/task-E2E workers and consumer SDK via normal release/drain procedures; verify actual capability, quota, cleanup and forbidden-purpose readback before instruction/default activation. Ledger: installed version/policy and rollout outcomes.
- [ ] 4.5 Complete Benchday companion agent-interface and changed-assertion acceptance, record alternating cold/warm performance with known/unknown phase data, and archive this change only after all tasks finish. Ledger: evidence references and delivered savings separate from baseline estimates.
