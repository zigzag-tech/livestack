## ADDED Requirements

### Requirement: Spending authority is granted deliberately, never by default

A broker SHALL be unable to provision until an operator declares pools with a
price and a per-pool ceiling. An absent, empty or malformed declaration SHALL
leave the broker planning and admitting exactly as before, provisioning nothing,
and SHALL say so at startup.

#### Scenario: A deploy that configures nothing
- **WHEN** a broker starts with no `LIVESTACK_FLEET_POOLS`
- **THEN** it plans and admits as before, `GET /fleet` reports no pools, and no operation can be claimed

#### Scenario: A malformed declaration
- **WHEN** the declaration does not parse, has a duplicate id, or names an unknown key
- **THEN** NO pool is configured, the startup line says why, and the broker does not burst — never a partially-parsed pool set

### Requirement: A recorded selection is not an acted-on selection

While the incident classifier is in `shadow`, its `selection` SHALL be persisted
and SHALL NOT influence any effect. Promotion to `serve` SHALL require a
published qualification receipt on frozen, grouped cases with confirmed labels,
and SHALL be a separate explicit activation.

#### Scenario: A shadow selection during a real incident
- **WHEN** the classifier returns a class for an unregistered incident on the live fleet
- **THEN** the selection, its submitted order and its probabilities are recorded with `applied: false`, and the deterministic path handles the incident

#### Scenario: A promotion attempted without a receipt
- **WHEN** a `serve` request is made against a profile with no qualification for this task and version
- **THEN** it refuses before dispatch, and evaluation remains available

### Requirement: First operations are evidence, not a smoke test

The first real provision and the first real drain SHALL each leave a record that
answers what was asked, what was known, what was chosen and why, why each loser
lost, and what happened — joinable to the admit record and the lease by
`job_id`.

#### Scenario: Reconstructing the first burst a month later
- **WHEN** an operator queries the ledger by the `job_id` of the first provisioned job
- **THEN** one query yields the admit decision with its candidate set, the lease id, every operation transition, the correlated announce and the eventual release

### Requirement: A queued job is not a dead end

A job the broker answers with `Queue` SHALL be reachable by the supervision
loop's next plan, so that demand the scheduler could satisfy by provisioning
actually reaches a provisioning decision. Which process owns that queue SHALL be
stated, with its reasons, before it is built.

#### Scenario: A job queued because the fleet is full
- **WHEN** `POST /fleet/admit` answers `Queue` for a job whose SLA tolerates the provision latency of a feasible pool
- **THEN** that job appears in a subsequent plan and produces a `provision` action, joined to its admit record by `job_id`

#### Scenario: A job queued under an SLA no pool can meet
- **WHEN** an `interactive` job is queued and every pool's provision latency exceeds its deadline slack
- **THEN** it stays queued and no operation is claimed for it — a promise the fleet cannot keep is not kept by spending
