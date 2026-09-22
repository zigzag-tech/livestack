## ADDED Requirements

### Requirement: The classifier classifies; code chooses the workflow

For an incident with no registered workflow, the system MAY ask Simple Jev (on the
model Harmony already serves, via `POST /v1/classifier`) one `choice` question
`failure_class` over `capacity_shortage`, `request_or_workload_fault`,
`provider_fault`, `uncertain_effect`, `needs_investigation`. Code SHALL map the
accepted class to exactly one registered workflow. A classifier answer SHALL NOT
select a tier, provision LAST_RESORT, or change region, quota or budget.

#### Scenario: Class maps to reconcile
- **WHEN** the accepted class is `uncertain_effect`
- **THEN** `reconcile_operation` runs; no create is issued

#### Scenario: Needs investigation
- **WHEN** the accepted class is `needs_investigation`
- **THEN** a full repair turn opens with the closed handback surface, and may end in `human_gate`

### Requirement: The incident packet is versioned, bounded and complete

The `state` sent to the classifier SHALL carry a schema version, stable
job/operation/attempt ids, principal and owner, absolute times and snapshot ages,
operation state and idempotency key, provider ids, eligible pools with exclusion
reasons, active and pending reservations, owner and prefix quota usage, structured
error with a bounded excerpt, policy versions, and spend/ETA with `unknown`
preserved. Required evidence SHALL refuse rather than trim, counted with the served
tokenizer against the compiled prompt.

#### Scenario: Packet exceeds the context budget
- **WHEN** required evidence does not fit the compiled prompt
- **THEN** the decision is refused with a named reason and the incident proceeds to full repair

### Requirement: Acceptance is a versioned policy; invariants leave no runner-up

Acceptance SHALL be a versioned, calibrated policy over the returned `confidence`
(normalised over offered labels). An invariant rejection SHALL return `violations`
with no selection; code SHALL NOT take the runner-up.

#### Scenario: Invariant rejects the winning class
- **WHEN** the classifier's winner violates a code invariant
- **THEN** the result carries `violations`, no workflow is selected, and the incident proceeds to full repair

### Requirement: Shadow produces no effects

In `shadow` mode the leaf's `selection` SHALL be recorded and SHALL NOT be acted on;
the deterministic path handles the incident. Promotion to `serve` SHALL require a
qualification receipt on frozen, grouped cases with a balanced permutation schedule
and sealed holdout, reporting accepted-decision correctness, dangerous-action
errors, abstention/coverage, invariant rejections, order disagreement and cascade
cost/p95 including fallback, and SHALL be a separate explicit activation.

#### Scenario: Shadow selection
- **WHEN** the leaf runs in `shadow` and returns `capacity_shortage`
- **THEN** the selection is persisted with its trace and no workflow is triggered by it

### Requirement: Classifier outages degrade to a human block

`unavailable`, `invalid_output`, `abstained`, `cancelled` and `deadline_exceeded`
SHALL keep distinct reasons, and an unavailable classifier SHALL resolve to a
durable human block without recursive provisioning.

#### Scenario: Classifier node is down
- **WHEN** `/v1/classifier` is unreachable for an unfamiliar incident
- **THEN** the outcome is `unavailable`, a human block is recorded, and no provisioning is attempted to restore the classifier

### Requirement: The decision is arguable a month later

The system SHALL persist, joined by `operation_id`, `decision_id` and the
`RepairRecord`: the evidence or a durable reference plus digest, question and
option order, label mapping, task/profile/model/template/order/acceptance versions,
raw result, invariant feedback, executed workflow, postcondition and eventual
job/cost outcome. Unobserved serving metadata SHALL be recorded as missing.

#### Scenario: Reconstructing one decision
- **WHEN** an operator queries by `operation_id` a month after the incident
- **THEN** one join yields the evidence digest, the exact option order, the raw probabilities, the versions in force, the workflow executed and the job's eventual outcome
