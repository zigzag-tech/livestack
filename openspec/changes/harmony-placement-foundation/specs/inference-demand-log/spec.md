## ADDED Requirements

### Requirement: Every forwarded request leaves a demand record

The node SHALL write one demand record for every request it forwards to a unit. The
record SHALL carry the time, unit, `composition_hash`, adapter (or null for the base),
principal namespace, requirement hash, prompt and completion tokens, the number of
samples requested (`n`), elapsed time, queue time, and outcome class.

#### Scenario: Multi-sample request
- **WHEN** the hub's chip call asks for `n: 12`
- **THEN** its one demand record carries `n: 12`, matching the twelve requests vLLM
  counts for it

#### Scenario: Adapter request
- **WHEN** a classifier call is served by `llm_general` through adapter `jemm`
- **THEN** the demand record names `adapter: "jemm"` and the unit's current
  `composition_hash`

### Requirement: Unknown fields are null, never zero

A token count or queue time the node could not obtain SHALL be recorded as `null`. It
SHALL NOT be recorded as 0.

#### Scenario: Streamed response without usage
- **WHEN** a streamed completion ends without a `usage` block
- **THEN** its record has `completion_tokens: null`

### Requirement: The demand log is bounded by size and age, and fails closed

The demand log SHALL rotate by size, keep a bounded number of files, and delete records
older than its age window. If the age window is unset, the writer SHALL refuse to start
and report why. It SHALL NOT run unbounded.

#### Scenario: Age window unset
- **WHEN** the node starts with the demand-log age window unset
- **THEN** demand logging is disabled, `/residence` reports `demand_log: "disabled: no
  age window"`, and requests are still served

### Requirement: Logging never slows a request, and a drop is never silent

Demand records SHALL be written off the request path through a bounded queue. A record
dropped because the queue is full SHALL increment a counter reported on `/residence`.

#### Scenario: Disk stall
- **WHEN** the log's disk stalls and the queue fills
- **THEN** requests continue at normal latency
- **AND** `/residence` reports a nonzero `demand_log_dropped`

### Requirement: The log identifies applications, not people

The demand record SHALL carry the caller's principal namespace (for example `benchday:`),
not the owner id.

#### Scenario: Hub request on behalf of an account
- **WHEN** the benchday hub calls with owner `benchday:acct_123`
- **THEN** the record's `owner_ns` is `benchday:`, and no field contains `acct_123`
