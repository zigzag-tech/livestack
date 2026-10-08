# work-scopes Specification

## Purpose
TBD - created by archiving change work-scopes-and-cascade-cancel. Update Purpose after archive.

## Requirements

### Requirement: A job may belong to a closable scope

A submission MAY name a scope `(owner principal, key)` with an optional lease. The authority SHALL record the scope on
the job, open the scope on first use, and report it in the job view. A submission without a scope SHALL behave exactly as before.

#### Scenario: First scoped submission opens the scope
- **WHEN** a principal submits a job naming scope `run-7`
- **THEN** scope `run-7` is `open` and `GET /v1/scopes/run-7` counts one queued job

#### Scenario: Another principal cannot see or close the scope
- **WHEN** a different principal reads or closes `run-7`
- **THEN** the answer is `404 scope_not_found` and nothing changes

### Requirement: Closing a scope cancels all its non-terminal work atomically

Closing a scope SHALL, in one transaction, cancel every `queued` or `running` job in it using the same semantics as owner
cancel, leave terminal jobs unchanged, and be idempotent. The result SHALL state how many jobs were cancelled, how many
attempts are held in worker cleanup and how many jobs were already terminal.

#### Scenario: Running and queued jobs
- **WHEN** a scope with one running and two queued jobs is closed
- **THEN** all three end `cancelled` with cause `scope_closed`, the running attempt is in `cleanup`, and the result reports `cancelled: 3, running_cleanup: 1`

#### Scenario: Repeat close
- **WHEN** the same scope is closed again
- **THEN** the result has `replayed: true` and no row changes

#### Scenario: Terminal work is untouched
- **WHEN** a scope containing a `succeeded` job is closed
- **THEN** that job's row is unchanged and counted as `already_terminal`

#### Scenario: Close before submit
- **WHEN** a scope is closed before any job names it and a job is then submitted into it
- **THEN** the submission is refused `409 scope_closed`

### Requirement: A closed scope accepts no work, including replays

A submission naming a closed scope, including a replay of an existing idempotency key whose job belongs to it, SHALL be
refused with `409 scope_closed` and SHALL NOT return the job.

#### Scenario: Retry loop after cancel
- **WHEN** a submitter replays the key of a job cancelled by scope close
- **THEN** it receives `409 scope_closed`, not the cancelled job

### Requirement: A scope lease expires abandoned work

A scope with a lease SHALL be closed with reason `lease expired` by the first authority transaction after
`lease_expires`, through the same cascade. Renewing SHALL extend the lease of an open scope and SHALL be refused
`409 scope_closed` for a closed scope. A scope without a lease SHALL NOT expire.

#### Scenario: Submitter dies
- **WHEN** a scope's lease passes without renewal
- **THEN** its non-terminal jobs end `cancelled`, the scope reports `close_reason: lease expired` and `closed_by: authority`

#### Scenario: Renewal after loss
- **WHEN** the submitter renews a scope the authority already closed
- **THEN** it receives `409 scope_closed` naming the close reason

### Requirement: Scope storage and work are bounded

Open scopes per owner, jobs per scope and retained closed scopes SHALL be bounded, and each bound SHALL be enforced by
refusal with a named error, never by silent eviction. A closed scope row SHALL be deleted only after no job references
it and the terminal retention window has passed.

#### Scenario: Scope full
- **WHEN** a scope already holds `scope_jobs` jobs
- **THEN** the next submission is refused `429 scope_capacity`

### Requirement: Every close leaves a record

A close that changes any job SHALL log one line and write one decision-ledger record naming the owner, key, actor, reason and counts.

#### Scenario: Lease expiry is attributable
- **WHEN** the authority closes an expired scope
- **THEN** the ledger record has actor `authority` and reason `lease expired`
