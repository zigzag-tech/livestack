# unit-composition Specification

## Purpose
What a card should run (base, adapters, KV dtype, context, batch cap): feasibility in code, cost by replay, deciders that propose and never apply, and the record every decision leaves.

## Requirements

### Requirement: Feasibility is code, never a parameter

`feasible(state, composition)` SHALL return feasible, infeasible with a reason, or
unknown with a reason. It SHALL evaluate the memory budget, KV tokens against
`max_model_len`, adapter-base agreement and rank, KV-dtype availability on the host's
engine, and context coverage of the demand trace. No tunable weight SHALL change its
result.

#### Scenario: Second adapter with bf16 KV on a 3090
- **WHEN** the state holds the 2026-09-28 measurements for `llm_general` and the
  candidate is `{chips-settinghead-v1, jemm}` with `kv_dtype: auto` and `max_model_len:
  24576`
- **THEN** the result is infeasible with reason `kv_tokens<max_model_len`

#### Scenario: Same adapters with fp8 KV
- **WHEN** the candidate is the same with `kv_dtype: fp8`
- **THEN** the result is feasible, and the predicted memory is within the margin of the
  measured row

### Requirement: No measured neighbour means unknown, not feasible

A memory prediction for a composition with no exact measurement SHALL be built from
measured deltas and marked `estimated`. If any term has no measured basis, feasibility
SHALL be unknown. It SHALL NOT be feasible.

#### Scenario: Never-measured base model
- **WHEN** a candidate puts a base model with no measurement on any host onto a device
- **THEN** the result is `Unknown(no_measured_basis:weights)`

### Requirement: Every decider is judged by the same functions

Every composition decider SHALL implement `propose(state) -> [Composition]`. Its
candidates, plus the live composition, SHALL be scored by the same `feasible` and
`cost`. A decider SHALL NOT write the ledger, read the ledger, or apply a composition.

#### Scenario: A proposer returns an infeasible candidate
- **WHEN** a decider proposes a composition that exceeds the device budget
- **THEN** that candidate is recorded as `filtered:infeasible` with its reason, and it
  is never chosen

### Requirement: Keep the live composition unless the gain exceeds the change cost

A run SHALL choose a new composition only when its cost is lower than the live
composition's by more than the change cost. Otherwise it SHALL choose `keep`.

#### Scenario: Marginal improvement
- **WHEN** the best feasible candidate saves less queueing than the expected restart
  downtime costs
- **THEN** the decision's `chosen` is `keep`, and the candidate is recorded as ranked

### Requirement: HARD_PIN units are never composed away

A composition that omits or changes the base model of a unit whose residency is
`HARD_PIN` SHALL be filtered in code with reason `filtered:hard_pin`.

#### Scenario: Jingway's pinned unit
- **WHEN** a candidate drops the HARD_PIN unit serving Jingway to free memory
- **THEN** it is filtered as `filtered:hard_pin`

### Requirement: Every composition run leaves one replayable ledger decision

A composition run SHALL write one ledger decision carrying its candidates (feasibility,
reason, cost breakdown), the chosen composition, the decider name and version, the
weights artifact hash, and the snapshot hash. Re-running the same decider on the stored
snapshot with the same weights SHALL reproduce the same decision.

#### Scenario: Replay
- **WHEN** a recorded composition decision is replayed from its snapshot and weights
- **THEN** the chosen composition and every candidate's feasibility are identical

### Requirement: Outcomes are joined to the decision that caused them

When a unit first reports a measured cost for a `composition_hash` that a decision
chose, the host broker SHALL write an outcome row with predicted-vs-measured memory per
term, keyed by `parent_decision_id`. For the first 24 hours after apply it SHALL write
hourly served-outcome rows. A chosen composition never measured within 7 days SHALL get
an outcome of `not_applied`.

#### Scenario: Prediction error recorded
- **WHEN** the chosen composition predicted 0.45 GiB for a second adapter and the
  engine measured 0.98 GiB
- **THEN** the outcome row records both values and the error, under the decision's id

#### Scenario: Ignored proposal
- **WHEN** no unit ever reports the chosen `composition_hash`
- **THEN** after 7 days the decision gets `outcome: not_applied`

### Requirement: A `plan()` decision can be replayed from its record

Every `plan()` action record SHALL reference a content-addressed snapshot of the
`WorldState` it was decided on, including footprints with their source, device
capacities and measured free memory. Re-running `plan()` on that snapshot SHALL
reproduce the recorded actions, and a mismatch SHALL be counted and reported.

#### Scenario: Snapshot replay
- **WHEN** `plan()` evicted `llm_title` to load `llm_general` and the record's snapshot
  is reloaded
- **THEN** `plan(snapshot, policy)` returns the same `Evict` and `Load`

### Requirement: Applying a composition is gated

Applying a chosen composition SHALL be gated on:
- the engine reporting ready within its deadline;
- measured memory within the prediction's margin;
- measured KV tokens at least `max_model_len`;
- the first requests served `ok`.

A gate that stays red SHALL restore the previous composition. In this change, applying
remains a person's action, and these gates are what they (and a later routine) check.

#### Scenario: Engine fails to start
- **WHEN** the new composition's engine exits during startup, as fp8 KV did against
  gcc-15 on 2026-09-28
- **THEN** the ready gate is red, the previous units file is restored, and the failure is
  recorded as the decision's outcome
