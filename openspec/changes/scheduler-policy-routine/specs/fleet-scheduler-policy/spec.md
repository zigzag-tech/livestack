## ADDED Requirements

### Requirement: The target choice is a compiled policy with a preserved reference

The fleet scheduler SHALL make each job's target choice through the policy
`livestack.fleet.choose_target` (family version 1), whose parameters come from a
versioned artifact. The Python implementation SHALL remain as the reference, and with the
default parameters the scheduler's plans SHALL be identical to those before this change.

#### Scenario: Defaults change nothing
- **WHEN** the 5 000-state golden corpus recorded before the refactor is replayed through `schedule()` with no artifact
- **THEN** every plan summary and action tuple is byte-identical to the recording

#### Scenario: Native and reference agree
- **WHEN** 10 000 generated cases with random in-bounds params are evaluated by the native module and the reference
- **THEN** eligibility, reason codes and greedy choice are identical and scores differ by at most 1e-12

### Requirement: Hard guards are code, never parameters

The last-resort guard, selector matching, deadline feasibility and capacity checks SHALL be
evaluated before any score, SHALL NOT depend on any tunable parameter, and exploration SHALL
never choose a candidate that would provision capacity or is in the last-resort tier.

#### Scenario: Cheaper tier feasible
- **WHEN** a LOCAL target and a LAST_RESORT pool are both feasible under any in-bounds params
- **THEN** the LAST_RESORT row is ineligible with reason `filtered:last_resort_guard`

#### Scenario: Exploration cannot spend
- **WHEN** exploration is enabled and an elastic pool scores within the margin of the greedy running target
- **THEN** the pool is not in the explore set and over 100 000 decision ids it is never chosen unless it is the greedy choice

### Requirement: Exploration applies only where a choice is committed once

Exploration SHALL apply only on `POST /fleet/admit`. `POST /fleet/plan` SHALL decide greedily
and SHALL report that exploration is off on the plan path. The first artifact SHALL have
exploration disabled.

#### Scenario: Repeated planning is stable
- **WHEN** the same queued job is planned on ten consecutive ticks with exploration enabled in the active artifact
- **THEN** all ten plans choose the same target

### Requirement: Every committed choice is recorded with what replay needs

Each `/fleet/admit` decision that chose a target SHALL be written, without blocking the request, as a
`jingway.policy_decision/v1` record to the bounded policy record stream, carrying the
artifact version, family, full context and candidate features, rows, greedy and chosen ids,
explore set, propensities, exploration settings, `self_traffic` and any shadow choices. The
existing admit ledger record SHALL carry a pointer with the same decision id. The response
SHALL include the `decision_id`, minted before deciding. When the recorder cannot write, or
the native module is absent, the broker SHALL report it as degraded.

#### Scenario: Record replays to itself
- **WHEN** the replay CLI self-checks a day of admit records
- **THEN** every record reproduces its logged rows, greedy, chosen and propensities

#### Scenario: Refusal is not recorded in the stream
- **WHEN** an admit finds no eligible target, or is refused for quota
- **THEN** no policy stream record is written, the audit ledger records it as before, and `skipped_no_choice` increments

#### Scenario: Recorder saturated
- **WHEN** the record stream's writer stalls and its queue fills
- **THEN** admits keep answering at their normal latency, dropped records are counted, a gap record is written when the writer recovers, and `/fleet` reports degraded

#### Scenario: Self traffic flagged
- **WHEN** an admit's authenticated principal is listed in `LIVESTACK_POLICY_SELF_PRINCIPALS`
- **THEN** its record has `self_traffic: true`

### Requirement: The job's outcome is joined to its decision

When a hosted lease granted by `/fleet/admit` is released or expires, the broker SHALL
append outcome records to the policy record stream keyed by that grant's decision id: `lease_held_s` and
`lease_expired` always, plus `caller_ok` and `job_wall_s` when the caller reported them at
release. An outcome SHALL be appended, never written into the decision record.

#### Scenario: Caller reports at release
- **WHEN** a caller releases its lease with `{"status": "ok", "wall_s": 41.2}`
- **THEN** the ledger gains outcome records `lease_held_s`, `lease_expired=0`, `caller_ok=1`, `job_wall_s=41.2` with the admit's decision id

#### Scenario: Lease expires
- **WHEN** a lease is reaped for missing heartbeats
- **THEN** outcome records `lease_expired=1` and `lease_held_s` are appended, and no `caller_ok` is invented

### Requirement: Only a validated, authorised artifact changes routing

The broker SHALL accept a policy artifact only through `PUT /fleet/policy/{policy_id}` from
a principal with `policy_admin`, only after the native module validated it and recomputed
its version, and SHALL write it atomically while keeping the previous one. A file that fails
validation SHALL leave the previously loaded artifact in force and SHALL be reported.

#### Scenario: Fleet auth off
- **WHEN** fleet auth is not configured and an artifact is PUT
- **THEN** the broker answers 403 and routing is unchanged

#### Scenario: Native module unavailable
- **WHEN** an artifact is PUT on a broker without the native module
- **THEN** the broker answers 503 and does not write the file

#### Scenario: Out-of-bounds param
- **WHEN** an artifact sets `w_budget` to 11
- **THEN** the broker answers 422 listing `param_out_of_bounds` and every other violation

#### Scenario: Revert without a model
- **WHEN** `POST /fleet/policy/{policy_id}/revert` is called
- **THEN** the previous artifact becomes active within one reload interval and no inference is requested

### Requirement: Degradation is visible

The broker SHALL report the policy's source, version, native mode, mismatch count and last
load error on `GET /fleet` and `GET /fleet/policy/{policy_id}`, and SHALL report degraded
when running on defaults, when native and reference disagree, when an artifact failed to
load, or when the native module is expected but absent.

#### Scenario: Compare mode mismatch
- **WHEN** in `compare` mode native and reference choose differently for one decision
- **THEN** the reference's choice is acted on, the mismatch count is 1, the case is written to the mismatches directory, and `/fleet` reports degraded

### Requirement: Shadow candidates never affect routing

The broker SHALL evaluate at most two shadow artifacts greedily beside the active one and
record their choices, and a shadow SHALL never change the chosen target, the response, or
capacity accounting.

#### Scenario: Shadow disagrees
- **WHEN** a shadow artifact would choose a different target than the active one
- **THEN** the response and lease name the active choice, and the record's `shadow` lists the other
