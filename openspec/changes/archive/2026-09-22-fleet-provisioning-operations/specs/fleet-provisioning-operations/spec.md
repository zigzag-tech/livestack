## ADDED Requirements

### Requirement: A provisioning operation is durable and idempotent

The system SHALL record a provisioning operation in a durable store before calling
a provider, with a stable `operation_id`, an idempotency key, the authorising
principal and delegated owner, and SHALL advance it only through the states
`intent`, `creating`, `created`, `rejected`, `uncertain`, `announced`, `failed`,
`released`. One logical operation SHALL result in at most one billed create.

#### Scenario: Provider reply lost after create
- **WHEN** the provider call times out or returns an ambiguous result
- **THEN** the operation is `uncertain`, a ledger record names it, and no retry is issued until `reconcile` resolves it to `created` or `rejected`

#### Scenario: Process restarts mid-create
- **WHEN** the broker restarts with an operation in `creating`
- **THEN** it reconciles that operation against the provider by idempotency key before issuing any new create

### Requirement: A claim is atomic against quota and pending work

The system SHALL admit a `Provision` only through a claim that atomically checks the
owner's quota, every enclosing prefix ceiling, pending creates and current
admissions under one writer lock, and SHALL refuse with a ledgered reason otherwise.

#### Scenario: Two concurrent claims at the quota boundary
- **WHEN** two operations claim the last permitted instance for one owner concurrently
- **THEN** exactly one is `creating` and the other is `rejected` with reason `quota`

#### Scenario: Region outside the caller's policy
- **WHEN** a proposed action would place in a zone outside the job's region policy
- **THEN** the claim is refused; the model or workflow cannot relax the policy

### Requirement: Announced means correlated

An operation SHALL reach `announced` only when a node carrying that `operation_id`
announces and its capability reports `ready`.

#### Scenario: An unrelated node becomes fresh
- **WHEN** a node without the operation's id announces during the operation
- **THEN** the operation stays in its current state

### Requirement: Deprovision is drain-gated

The system SHALL release a worker only after a drain claim, zero active leases and
jobs on it, no pending admission targeting it, and a final authoritative re-check.
`schedule()`'s `Deprovision` action is a proposal and SHALL NOT release by itself.

#### Scenario: Scheduler proposes deprovision of a busy worker
- **WHEN** the plan contains `Deprovision` for a node holding an active lease
- **THEN** the operation is refused with reason `busy` and the node stays

### Requirement: The planning API reserves nothing and hides nothing

`POST /fleet/plan` SHALL return a serialized plan over stable job ids with eligible
pools and their exclusion reasons, current reservations and the policy version,
SHALL preserve `unknown` for unreported capacity, spend or ETA, and SHALL NOT
reserve capacity.

#### Scenario: Unreported in_flight
- **WHEN** a target reports no `in_flight`
- **THEN** the plan marks that target's capacity `uncertain` rather than free

### Requirement: Every transition is ledgered

Every operation transition and claim outcome SHALL be recorded in the existing
decision ledger, joinable to admit and placement records by `job_id`, and a failed
ledger write SHALL mark the operation `observability_degraded` rather than pass silently.

#### Scenario: Ledger directory unwritable
- **WHEN** a transition's ledger record cannot be written
- **THEN** the transition still applies, the operation carries `observability_degraded`, and `GET /fleet` reports it
