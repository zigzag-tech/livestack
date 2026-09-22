## ADDED Requirements

### Requirement: The loop supervises; it does not decide placement

The fleet supervision loop SHALL obtain every placement from `POST /fleet/plan` and
every effect through `POST /fleet/operations`, and SHALL hold no capacity or
reservation state of its own.

#### Scenario: Loop restarts
- **WHEN** `fleetd` restarts during an operation
- **THEN** the next tick observes the operation's state from the API and no create is repeated

### Requirement: Gates are correlated facts

Each operation step's gate SHALL read the operation's own state and the correlated
node capability, not the presence of a fresh node or the HTTP status of the dispatch.

#### Scenario: Green path costs nothing
- **WHEN** every operation reaches `announced` within its deadline
- **THEN** the tick completes with zero escalations and zero inference tokens

### Requirement: Known errors never reach a model

A red gate SHALL first consult a registered workflow table keyed by structured
error `{stage, class, code}`; a matching code SHALL run its workflow without
opening any escalation. Provisioning-in-progress and policy-driven waiting SHALL
be represented as states with a wakeup, not as red gates.

#### Scenario: Provider returns a known capacity code
- **WHEN** the create fails with a code registered as `capacity_shortage`
- **THEN** `refresh_availability` runs with a capped, scoped cooldown and the job is re-planned; no classifier or repair turn is invoked

### Requirement: The effect surface is closed

Handbacks available to any escalation SHALL be typed, SHALL be limited to
`read_fleet_view`, `read_operation`, `read_ledger_since`, `reconcile_operation`,
`refresh_availability`, `schedule_wakeup`, `request_policy_change`, and SHALL NOT
include any mutation of region policy, quota, budget or tier selection.

#### Scenario: Escalation asks to relax region
- **WHEN** a repair turn calls `request_policy_change('region', reason)`
- **THEN** a request is recorded for a person; the job's policy is unchanged

### Requirement: Repairs do not serialise unrelated jobs

Escalations SHALL open in a conversation bound per job, and a blocked operation
SHALL NOT stop admission to healthy existing workers.

#### Scenario: One job blocked on human_gate
- **WHEN** job A's operation is blocked awaiting a person
- **THEN** job B is planned and admitted to an existing worker on the same tick
