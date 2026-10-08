## Purpose

Give agents reusable, owner-scoped execution environments through Harmony's ordinary workload requests while preserving per-execution queue admission, immutable inputs, bounded storage and attributable results.

## ADDED Requirements

### Requirement: Ordinary job submissions resolve a persistent environment identity

Schema-3 submissions SHALL accept an optional environment reference containing exactly one task key or opaque handle and `reuse: prefer`. The same authorized owner and key SHALL resolve to one logical environment while its metadata is retained. Jobs SHALL keep separate idempotency identities and immutable captured inputs. Resolution SHALL grant no execution resources. Versions 1/2 SHALL preserve their current canonical identities and reject unsupported fields.

#### Scenario: An agent tests a new edit
- **WHEN** an owner submits a new job/input with its existing environment key or handle
- **THEN** the new job names the same logical environment and queues for fresh admission
- **AND** its source/result identity belongs to the new captured input

#### Scenario: A submission response is lost
- **WHEN** the caller repeats the identical job key and request
- **THEN** it receives the existing job and environment handle without another execution
- **AND** different inputs or environment references under that job key conflict

#### Scenario: Two owners choose the same task key
- **WHEN** distinct authorized ownership scopes use an identical environment key
- **THEN** they receive distinct environments and cannot inspect or mutate each other's state

### Requirement: Environment support is negotiated and exposed through existing agent interfaces

Authenticated capability discovery SHALL report supported schema versions and environment-enabled handlers. The workload SDK and existing submit/get CLI SHALL support environment key/handle selection, environment inspection and structured job observation. Unsupported explicit reuse SHALL produce `environment_unsupported` before source upload, without silent downgrade or unmanaged execution. CLI/help/runbooks SHALL explain the separation of task environment identity and job idempotency identity.

The authority SHALL reload an explicitly supplied `environment_handlers` policy map on SIGHUP without restarting or interrupting active attempts. A config that omits this section SHALL preserve the current map; an explicit empty map SHALL disable environment enrollment. Invalid replacement policy SHALL leave the prior map and other reloadable sections unchanged.

#### Scenario: An updated agent reaches an older authority
- **WHEN** explicit environment selection is unsupported or capability discovery fails
- **THEN** the caller reports the unsupported/unavailable state and starts no compiler
- **AND** disposable execution requires an explicit opt-out through normal admission

#### Scenario: A replacement agent observes accepted work
- **WHEN** it uses the handoff's accepted job ID and environment reference
- **THEN** it observes the same job through get and can inspect the environment without creating another job

#### Scenario: An operator changes environment enrollment during service
- **WHEN** the operator reloads valid environment handler policies while jobs are active
- **THEN** fresh capability reads reflect the new eligible and forbidden handlers without interrupting those jobs
- **AND** an invalid reload leaves the old capability and admission policy in place

### Requirement: Every environment execution acquires and releases ordinary resources

Every execution SHALL enter the existing workload queue, satisfy the current physical-host resource/policy checks and hold an authorized writer generation. Environment affinity SHALL confer no priority or concurrency exemption. An environment SHALL become parked only after owned process/runtime cleanup is confirmed; parked state SHALL retain disk only and hold no CPU/RAM reservation, running-count charge or heartbeat.

#### Scenario: Another task needs resources after a build finishes
- **WHEN** the build's owned processes have stopped and its environment is parked
- **THEN** CPU/RAM becomes available to other queued work while bounded files remain

#### Scenario: A compiler descendant survives cancellation
- **WHEN** an owned compiler process remains after cancellation
- **THEN** cleanup remains pending, capacity is not advertised as free and no new writer reuses that replica

### Requirement: Environment execution stops at task-specific E2E

Environment-enabled execution SHALL be limited by installed policy to development compilation/checks/tests and nonempty explicitly scoped task E2E. The authority SHALL accept at most 64 unique exact task-E2E check IDs, optionally restricted by an installed finite allowlist. The installed handler SHALL resolve them against immutable captured source and refuse an empty selection, unknown IDs, or a selection that expands to the full suite before preparation or execution. Full/coalesced E2E and publishing/release handlers SHALL reject environment references with `environment_scope_forbidden`. Task E2E selection SHALL NOT expand or coalesce into full-suite execution. Existing full-test and publish orchestration SHALL continue without task environment bindings; caller metadata SHALL NOT grant a different purpose.

#### Scenario: A caller requests a full E2E run with its task handle
- **WHEN** the handler purpose is full E2E or the selection resolves to the entire suite
- **THEN** the request refuses before execution rather than applying the task environment

#### Scenario: A caller attaches a handle to a publishing job
- **WHEN** the installed purpose is publishing or release
- **THEN** it receives environment scope forbidden and cannot spoof development purpose

#### Scenario: An agent requests only its task checks
- **WHEN** an authorized task-E2E handler receives a nonempty proper subset of checks
- **THEN** it queues normally and can reuse compatible preparation with fresh runtime fixtures

### Requirement: Environment updates are exclusive and fenced across worker identities

At most one current authorized writer SHALL update a logical environment. Same-host replicas SHALL be serialized across worker identities. Stale attempt/boot/generation writes SHALL NOT advance environment state or satisfy a job. Restart and cancellation SHALL reconcile unfinished generations before reuse; an unconfirmed old replica SHALL be reconstructed elsewhere or visibly refused under existing retry/cleanup policy.

#### Scenario: Two jobs request one environment concurrently
- **WHEN** both jobs are eligible for the same environment
- **THEN** one writer is admitted and the other remains queued without reserving CPU/RAM
- **AND** unrelated environment jobs remain eligible

#### Scenario: A disconnected old worker returns
- **WHEN** a replacement generation has been authorized on another host
- **THEN** the old attempt cannot publish source/build state or results for that generation

#### Scenario: A worker returns with a superseded local replica
- **WHEN** its bounded report identifies a parked replica whose logical environment is missing or has advanced to another generation
- **THEN** the authority returns cleanup instructions bound to the exact reported handle and generation
- **AND** the worker removes that directory only while holding its per-handle lock and after confirming the marker still has that generation
- **AND** a busy lock is retried on a later report, while a changed marker or failed removal never deletes a newer or active generation and remains visible under the bounded worker-storage policy

### Requirement: Retained state cannot change executed source or artifact freshness

Workers SHALL execute the complete verified captured input of each job, including deletions, modes, symlinks and external dependencies. They SHALL preserve only declared compatible cache/output state and SHALL reset runtime/test state. Compatibility SHALL cover actual platform/toolchain/ABI, dependency and build-recipe identities. Unknown, corrupt or interrupted retained state SHALL cause a named rebuild/refusal. Important source/output data SHALL remain recoverable independently of environment caches.

#### Scenario: A source file was deleted in the next snapshot
- **WHEN** the next job reuses a workspace containing that old file
- **THEN** the obsolete source is removed before execution and cannot affect the result

#### Scenario: The native toolchain changes
- **WHEN** a retained target is incompatible with the admitted worker's toolchain/ABI
- **THEN** it is invalidated before use and the outcome states why rebuilding occurred

#### Scenario: A test passes after a previous test failure
- **WHEN** compatible compilation state is retained across the two jobs
- **THEN** test runtime/fixture data is reset and the new result identifies its own captured input

### Requirement: Reuse is a bounded placement preference

Placement SHALL apply hard eligibility and current capacity checks before reuse preference. Where measured estimates exist it SHALL consider expected finish time; absent estimates SHALL remain unknown. Affinity SHALL delay an otherwise eligible alternative by at most 15 seconds, durably measured from the first such alternative and never reset by replanning. Cold reconstruction on another eligible host SHALL preserve the logical handle and report relocation. Stored environments SHALL NOT prevent deprovision or cause paid provisioning by themselves.

#### Scenario: The old host is busy while another eligible host is available
- **WHEN** the bounded affinity window expires
- **THEN** ordinary placement can admit the alternative and reconstruct from captured inputs
- **AND** the outcome reports relocation and its reason

#### Scenario: Only an excluded laptop has the old files
- **WHEN** its policy forbids the requested compilation class
- **THEN** cache presence grants no permission and placement uses another eligible host or reports the real wait/refusal

### Requirement: Environment storage and metadata have enforced bounds

The system SHALL enforce the count/byte/age ceilings in the design: 1,024 logical rows globally, 64 per owner, two replicas per environment, 64 replicas per host, 32 GiB per replica, 128 GiB per owner per host, and a host total no larger than 256 GiB or the provisioned workspace budget. Child writes SHALL be kernel bounded. Inactive generations SHALL expire after seven idle days or thirty absolute days under configured nonzero windows; active generations and existing CAS retention exemptions SHALL be protected. Missing quota enforcement SHALL disable environment support, and deletion failures SHALL remain charged and visible.

#### Scenario: A compiler writes past its environment quota
- **WHEN** a child reaches the enforced filesystem byte limit
- **THEN** the write is refused, the job reports storage exhaustion and other owners' active state remains intact

#### Scenario: Idle files were evicted
- **WHEN** a job later requests the still-retained logical handle
- **THEN** it reconstructs from captured input and reports rebuilt rather than a cache hit

#### Scenario: Retention is unset
- **WHEN** an expiry configuration is absent or zero
- **THEN** that pass deletes nothing, reports the invalid retention state and refuses additional retention admission

### Requirement: Reuse outcomes and timing are attributable without entity fan-out

Job status/receipts SHALL name the environment handle/generation, requested and actual reuse, reason, compatibility/input identities and actual producing host/attempt. Waiting and cleanup states SHALL have separate reasons. Measured phase timings SHALL distinguish queue, transfer, preparation, build, execution and cleanup; unavailable readings SHALL be unknown with a reason. Metadata/receipts and decision records SHALL be bounded as in the design, with `observability_degraded` for recording failures. Registry/placement/sweep round trips SHALL be bounded independently of environment/entity count.

Each admitted placement SHALL have one stable `decision_id`, minted before the scheduler chooses a target. The ID SHALL be present in the worker assignment, attempt history and compilation authorization receipt, and those records SHALL agree. The terminal environment outcome SHALL remain joinable to that ID through its job/attempt/generation identity. A worker SHALL refuse a compilation authorization receipt whose `decision_id` differs from its journaled assignment; caller input SHALL NOT choose this identity. Legacy attempts without an ID may report it as unknown.

Handler timing traces SHALL be bounded and SHALL represent each dependency, compile and test phase as either a finite nonnegative duration or an unknown value with a bounded reason. During a rolling upgrade, the worker SHALL continue to accept the existing version-1 task-E2E trace while new handlers may emit the version-2 phase map.

#### Scenario: A caller requests reuse but the caches are incompatible
- **WHEN** the job succeeds after rebuilding
- **THEN** its product outcome is succeeded and reuse outcome is rebuilt with the invalidation reason

#### Scenario: Timing instrumentation is unavailable
- **WHEN** a phase's duration cannot be measured
- **THEN** the receipt reports unknown and does not count that interval as saved time

#### Scenario: Compilation authorization names a different placement decision
- **WHEN** the authority's compilation receipt decision ID differs from the worker's journaled assignment
- **THEN** the worker refuses before the handler can start a compiler

#### Scenario: Placement and completion share a bounded decision-ledger join
- **WHEN** a workload attempt is admitted and later completes
- **THEN** the bounded placement record uses the attempt's `decision_id`, and its completion event joins by `parent_decision_id`, job, attempt, environment handle and generation
- **AND** a failed ledger write is visible through the admin authority status as `observability_degraded` without refusing the workload

#### Scenario: The environment registry grows to its configured limit
- **WHEN** placement processes the bounded candidate set
- **THEN** lookup/claim operations use a bounded batch rather than one database round trip per environment
