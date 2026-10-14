## ADDED Requirements

### Requirement: Every terminal job carries a typed cause

Every job that reaches a terminal state SHALL carry a `cause` with a `kind` from a closed vocabulary, a `retry` advice
(`same`, `elsewhere`, `after_change` or `no`) and bounded `evidence`. A cause the authority cannot classify SHALL be
`unknown` with `evidence.unreadable`; it SHALL NOT default to a specific kind. Jobs that ended before this change SHALL report
`cause: null` with reason `predates_causes`.

#### Scenario: Cancelled by owner
- **WHEN** an owner cancels a running job
- **THEN** the job's cause is `cancelled_by_owner` with retry `no`

#### Scenario: Unknown kind from a newer worker
- **WHEN** a worker completes with a cause kind the authority does not know
- **THEN** the result is accepted, stored as `unknown`, and the original name appears in `evidence.reported_kind`

### Requirement: An OOM kill is named from kernel or systemd evidence even when no receipt exists

When an attempt's unit ends without a receipt, the worker SHALL classify the stop from the last sampled `memory.events` and
`pids.events` of the attempt cgroup, the systemd `Result` and `ExecMainStatus`, and the lease keeper's error, in that
precedence, and SHALL report `oom_killed` when `oom_kill` was observed or systemd reports `oom-kill`. A lost-lease reason
SHALL NOT be reported when the unit died of a resource kill.

#### Scenario: Wrapper killed with the handler
- **WHEN** the kernel kills the whole unit at its `MemoryMax` and no receipt is written
- **THEN** the attempt completes `infrastructure` with cause `oom_killed`, retry `after_change`, and evidence naming the source, the last peak and the limit

#### Scenario: Evidence unreadable
- **WHEN** the unit is gone and neither `memory.events` nor systemd `Result` can be read
- **THEN** the cause is `unknown` and `evidence.unreadable` lists the files that failed

#### Scenario: Genuine lease loss
- **WHEN** the authority refuses a heartbeat while the unit is alive
- **THEN** the cause is `lease_lost` with the refusal as evidence

### Requirement: Queued jobs state why they are not running

A queued job's view SHALL carry `placement` with the time the current blocker set first appeared and, per candidate worker,
a blocker `{worker, host, code, detail}` with `code` from a closed enum. `placement` SHALL be `null` only for a job never
evaluated, and an evaluation that found no worker SHALL say `no_workers`. It SHALL be rewritten only when its blockers change.

#### Scenario: One busy worker
- **WHEN** the only worker advertising the handler holds an active attempt
- **THEN** the job's placement has a blocker with code `worker_busy` and a stable `since`

#### Scenario: Steady wait
- **WHEN** placement is evaluated repeatedly with unchanged blockers
- **THEN** no write changes the job's placement other than `evaluated`, at most once a minute

### Requirement: Stall and queue-wait deadlines are opt-in and name their cause

A handler MAY declare a progress deadline and a submission MAY declare a maximum queue wait. When exceeded, the authority SHALL
end the attempt or job with cause `stalled_no_progress` or `unplaceable` respectively, carrying the last progress or placement.
Absent a declaration, no such deadline SHALL apply.

#### Scenario: Handler stops reporting progress
- **WHEN** a handler with a 600 s progress deadline reports an unchanged progress document for 601 s
- **THEN** the attempt is abandoned with cause `stalled_no_progress`

#### Scenario: Declared deadline without progress reporting
- **WHEN** a handler declares a progress deadline but does not report progress
- **THEN** handler loading refuses with the key named

### Requirement: Classification never fails silently

The worker SHALL log one line per terminal attempt naming the cause kind, retry advice and evidence source, and an exception
inside classification SHALL yield cause `unknown` with the classifier error in evidence.

#### Scenario: Classifier error
- **WHEN** reading the cgroup raises an unexpected exception
- **THEN** the attempt still completes, with cause `unknown` and `evidence.classifier_error`

### Requirement: A failed handler's cause names the decisive log line

For every non-succeeded completion the worker SHALL ship `result.log_excerpt`: one line of at most 300 bytes, `exit N: <first
error line of command.log>` (the last non-empty line when none matches), never a line that matches a secret pattern, with URL
credentials removed. The authority SHALL re-clean it (its bounds, not the worker's) and carry it as `cause.evidence.excerpt` for
`handler_failed` and `unknown`, and SHALL derive the job `reason` as `handler failed: <excerpt>` for `handler_failed`, so a job that
failed on landed code never reads as "failed, no reason". An unreadable log SHALL leave the excerpt absent, never invented.

The class rule is: `outcome=product_failure` iff `cause.kind=handler_failed` (landed code, or a product assertion, failed);
every other non-success is infrastructure (worker, harness environment, scheduling). Consumers SHALL take the class from the
authority's outcome and cause, never from the absence of an artifact.

#### Scenario: Landed script with a SyntaxError
- **WHEN** a handler runs a node script that fails with `SyntaxError: Identifier 'x' has already been declared`
- **THEN** the job ends `product_failure`, cause `handler_failed` with `evidence.excerpt` starting `exit 1: SyntaxError: Identifier 'x'`, and `reason` `handler failed: ` plus that excerpt
