# fleet-supervision-loop Specification

## Purpose
The loop that carries a plan's actions out, and the limits on what it may do.

It SUPERVISES; it does not decide. Every placement comes from the broker's
planning route and every effect goes through its operations route, so the loop
holds no capacity state and restarting it costs nothing.

Its gates read CORRELATED facts. The tempting gate for a provision is "did a
fresh node appear?", which is almost always true on a live fleet and therefore
proves nothing about the create that was just paid for. Its green path costs
nothing: deterministic gates, a registered workflow table consulted before any
model, and waiting expressed as a state with a wakeup rather than as a failure.
Its effect surface is closed, and closed deliberately — a surface that could
select the last-resort tier would bypass the guard that makes "last resort"
literal rather than merely expensive.

## Requirements

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

### Requirement: Fleet observations come from an authenticated bounded stream

FleetD SHALL obtain its fleet observation from an authenticated Hub subscription
to `harmony.fleet/1` with account scope. The subscription's authenticated
`account_id` SHALL match an explicitly configured expected account. FleetD SHALL
not request `GET /fleet` or use an HTTP fallback for fleet observations; the
existing planning and operation routes remain the authorities for placement and
effects.

The consumer SHALL hold no more than 256 partials, each no larger than 4,096
serialized bytes, in each committed or staged generation. While a replacement
cut is staged, it MAY retain both generations, bounded to 512 partials and 2 MiB
total; each generation SHALL remain within 1 MiB. It SHALL treat a warm cached
receipt (or a receipt without provenance) as stale until a live sample replaces
it. It SHALL accept an observation for planning only when coverage is complete
for a non-empty expected source set and every reporting partial receipt is at
most 20,000 ms old (or the partial's smaller TTL, if present). The authenticated
Hub envelope identity SHALL be `subject.machine`; `subject.node` is not the
envelope identity for this host adapter. It SHALL preserve the raw host-level
partial bodies and SHALL NOT invent worker rows absent from those bodies.

The observation SHALL expose distinct `unknown`, `stale`, `degraded`, and `gap`
states. FleetD SHALL stop before calling the planning route unless the current
observation is fresh and complete.

#### Scenario: Fresh complete account stream

- **WHEN** the authenticated resident account matches the configured account and a non-empty source set reports complete, fresh `harmony.fleet/1` coverage
- **THEN** FleetD supplies the stream-derived host facts to the supervision loop and may request `POST /fleet/plan`
- **AND** it issues no `GET /fleet` request

#### Scenario: Account identity does not match

- **WHEN** the authenticated resident reports an account ID different from the configured account
- **THEN** FleetD reports a degraded observation and makes no fleet subscription or planning request

#### Scenario: No initial complete sample

- **WHEN** the account-scoped subscription has not delivered a complete sample
- **THEN** FleetD reports `unknown` and does not call `POST /fleet/plan`

#### Scenario: A reporting source sample ages out

- **WHEN** any reporting stream receipt is older than the contract TTL or its smaller partial TTL
- **THEN** FleetD reports `stale` and does not call `POST /fleet/plan`

#### Scenario: Producer reports failure

- **WHEN** the subscription is refused or degraded, a partial is failed, denied, incapable, or partial, or the event consumer fails
- **THEN** FleetD reports `degraded` with the source reason and does not call `POST /fleet/plan`

#### Scenario: Coverage has a gap

- **WHEN** coverage is incomplete, the expected source set is empty or inconsistent, a stream reset occurs, or a bounded input is exceeded
- **THEN** FleetD reports `gap` and does not call `POST /fleet/plan`

#### Scenario: Fresh stream contains host summaries only

- **WHEN** a complete fresh partial contains host-level facts but no worker rows
- **THEN** FleetD preserves those facts and reports no synthetic worker rows
- **AND** placement remains the responsibility of `POST /fleet/plan`
