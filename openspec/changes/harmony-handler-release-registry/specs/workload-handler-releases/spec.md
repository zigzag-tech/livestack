## Purpose

This capability lets operators publish and activate compatible workload handler code independently while Harmony keeps durable authority, worker sessions, leases, and accepted job behavior intact.

## ADDED Requirements

### Requirement: Handler releases are immutable and operator authorized

Harmony SHALL accept a release only through an authenticated operator action and SHALL verify a closed versioned manifest, canonical content digest, complete file inventory, runtime identity, and handler contract before registering it. The manifest MAY provide a bounded fixed argument list for its relative entry point. Package metadata SHALL NOT grant permissions, select arbitrary executables or transport endpoints, or expand caller, host, signing, or compilation policy. Package extraction SHALL reject duplicate names, absolute paths, parent traversal, links, special files, undeclared files, and digest or size mismatches.

#### Scenario: An operator stages a valid package
- **WHEN** an authorized operator stages a package whose canonical manifest and complete file inventory verify
- **THEN** Harmony records its immutable handler ID, release digest, execution contract, payload and result schemas, runtime requirements, and declared outputs

#### Scenario: A package tries to escape its installation root
- **WHEN** a staged archive contains a traversal path, link, duplicate path, undeclared file, or mismatched digest
- **THEN** Harmony refuses it before registration, leaves the effective registry unchanged, and records the named validation refusal

#### Scenario: An untrusted caller tries to register a release
- **WHEN** a caller submits a release registration or activation request
- **THEN** Harmony refuses the request and preserves the operator-owned catalog and policy

### Requirement: Registry activation is atomic and available without core restart

An authorized operator SHALL stage, activate, and roll back releases through the running authority using an expected registry generation. The authority SHALL commit each complete validated registry generation atomically and SHALL expose a durable receipt that distinguishes desired, worker-effective, and authority-observed state. Workers SHALL observe compatible generations over their existing bounded control exchange without changing process identity, boot identity, active attempts, or lease renewal. A conflicting generation, invalid candidate, crash, or lost acknowledgment SHALL have an explicit result and SHALL NOT produce a partial registry.

#### Scenario: A compatible release activates while a job runs
- **WHEN** a worker executes release A and observes an activation selecting release B
- **THEN** the worker keeps the same PID and boot, renews A's lease, keeps A's attempt descriptor, and reports B only after it has verified and atomically installed B

#### Scenario: Concurrent activation uses a stale generation
- **WHEN** an operator activates a release against an expected generation that has already changed
- **THEN** Harmony returns a generation conflict and preserves the currently committed generation

#### Scenario: A worker crashes at the activation boundary
- **WHEN** a worker recovers with a crash before or after its durable registry pointer commit
- **THEN** it loads the complete previous or complete new generation respectively and names the effective generation in its next report

#### Scenario: An activation acknowledgment is lost
- **WHEN** an operator retries the same activation request ID after its response was lost
- **THEN** Harmony returns the original receipt without creating another generation or changing a job's pinned release

### Requirement: Accepted jobs preserve submitted intent and exact release identity

For release-aware submissions, Harmony SHALL resolve either an explicitly named installed digest or the selected default inside the job acceptance transaction. It SHALL persist the original submitted intent separately from the resolved release identity. Repeating an idempotency key with the same intent SHALL return the original job after a default changes; reusing that key with changed intent SHALL be refused. All attempts and infrastructure retries SHALL retain the accepted digest and contract identities.

#### Scenario: A default changes after a submission reply is lost
- **WHEN** a caller repeats the same default-based idempotency key after the default moves from A to B
- **THEN** Harmony returns the existing job pinned to A

#### Scenario: A caller changes release intent under an existing key
- **WHEN** a caller repeats an idempotency key but changes from default selection to an explicit release or selects a different digest
- **THEN** Harmony refuses the submission as changed intent and creates no second job

#### Scenario: An infrastructure retry follows activation
- **WHEN** an A attempt fails as infrastructure after B becomes default
- **THEN** the retried attempt remains pinned to A or waits explicitly for A and never substitutes B

### Requirement: Placement uses exact worker release inventories

Release-aware workers SHALL report bounded inventories of verified installed handler digests and supported contract versions. Harmony SHALL assign a pinned job only to a fresh eligible worker that advertises its exact release and contract. Missing, stale, unsupported, or conflicting inventory SHALL produce a named wait or refusal and SHALL NOT fall back to a handler name, newer default, or legacy worker. Each placement decision SHALL retain its requested and selected release identities and outcome in the durable workload record.

#### Scenario: Only legacy workers are available
- **WHEN** a release-aware job is queued and every eligible worker lacks its required release digest
- **THEN** the job remains unassigned with a named release-availability reason and no legacy worker executes it

#### Scenario: A worker withdraws an advertised package
- **WHEN** a worker no longer has the exact digest named by an assignment
- **THEN** it refuses the assignment visibly, and any re-placement retains the original digest

#### Scenario: A contract major is unsupported
- **WHEN** the installed core does not support the release's execution contract major
- **THEN** activation or placement names the unsupported contract and does not interpret it using another major

### Requirement: One pinned descriptor governs an attempt and its result

Harmony SHALL resolve one immutable release descriptor for each assigned attempt and SHALL use that descriptor for preparation, launch, exit classification, declared artifact selection, upload, cleanup, and recovery. The worker journal and authority attempt record SHALL carry the job, attempt, fence, worker boot, release digest, and contract identities. The authority SHALL bind completion to those stored identities and SHALL expose them with the terminal result; missing or mismatched identity SHALL not complete a release-aware job. Legacy jobs SHALL remain distinguishable and SHALL NOT be reported as release-pinned.

#### Scenario: A replacement changes exit and output policy
- **WHEN** release B changes infrastructure exit codes and output declarations while an A attempt runs
- **THEN** A's completion and artifact handling continue to use A's immutable descriptor through cleanup

#### Scenario: A worker recovers an interrupted attempt
- **WHEN** a worker recovers a journal for release A after the registry default changes to B
- **THEN** it verifies and resumes or cleans up A using the recorded fence and never re-resolves the job through B

#### Scenario: Completion reports the wrong digest
- **WHEN** a worker completion omits or changes the digest or contract identity bound to its assignment
- **THEN** the authority rejects completion and records no successful result for that attempt

### Requirement: Package staging and retention are bounded and reference safe

The authority and every worker SHALL enforce configured limits for handler IDs, releases per handler, bytes per package and in total, manifest metadata, files per package, staging entries, installed packages, and activation receipts. The worker package root SHALL contain at most 256 installed packages and 260 total entries, including at most three transient download/extraction/pointer entries; startup SHALL remove incomplete transients from a prior crash and refuse an over-capacity root by name. Reference reconciliation SHALL issue a bounded number of database round trips independently of job count. Accepted nonterminal jobs, attempts, defaults, rollback selections, journals, and pending cleanup SHALL protect required packages. Retirement SHALL stop future default selection without rewriting accepted work. Deletion SHALL require a configured unreferenced-age window and complete reference evidence; unknown evidence SHALL prevent deletion. Capacity exhaustion SHALL refuse staging visibly instead of deleting a referenced release.

#### Scenario: Referenced packages fill the byte budget
- **WHEN** staging a release would exceed the package cap and existing packages are still referenced
- **THEN** staging refuses with a named capacity result and preserves all referenced packages

#### Scenario: A new default activates during cleanup
- **WHEN** an operator changes the default from A to B while an A attempt still uploads artifacts or awaits cleanup
- **THEN** A remains installed and available to that attempt until all references and the configured age window permit collection

#### Scenario: Reference evidence is unavailable
- **WHEN** the authority or worker cannot establish the complete release reference set
- **THEN** it names the evidence failure and deletes no potentially referenced package

#### Scenario: Destructive retention is unconfigured
- **WHEN** the package garbage collector has no valid positive retention window
- **THEN** collection fails closed while package admission remains bounded and reports any capacity refusal
