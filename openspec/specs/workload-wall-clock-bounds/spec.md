# workload-wall-clock-bounds Specification

## Purpose
Shared capacity is not held indefinitely. The authority bounds how long one attempt may run and how long a job may
wait, so a job that outlives either is the owner's to split, never the fleet's to carry.

## Requirements

### Requirement: An attempt has an authority-wide wall-clock bound

The authority SHALL fence any running attempt whose wall-clock age exceeds `limits.max_attempt_seconds` (default 10800,
3 hours; configurable within [30, 86400]; it SHALL NOT be disableable) and SHALL fail its job with cause
`attempt_wall_clock_exceeded` and retry advice `after_change`. The attempt moves to `cleanup` and its worker is held
until clean, exactly as for an owner cancel. The clock is per attempt: a retried attempt starts its own.

#### Scenario: Inside and outside the bound
- **WHEN** an attempt has run 2h59m
- **THEN** its job is still `running`
- **WHEN** it reaches 3h01m
- **THEN** the job is `failed` with cause `attempt_wall_clock_exceeded`, the attempt is in `cleanup`, and a late heartbeat is refused

### Requirement: A submitter may only lower the bound

A schema-4 submission MAY carry `max_attempt_seconds` in [30, the authority's bound for that handler]. A larger value
SHALL be refused at submission. The only way to run longer is a named per-handler entry in
`limits.wall_clock_exceptions`, each with a `max_attempt_seconds` above the global bound (at most 86400) and a written
reason; the set is empty by default and is read at authority start.

#### Scenario: Raising is refused
- **WHEN** a submission asks for `max_attempt_seconds` above the handler's bound
- **THEN** it is refused naming the authority's bound

### Requirement: A queued job has an authority-wide age bound

A job `queued` longer than `limits.max_queue_age_seconds` (default 86400) SHALL expire with cause `queue_age_exceeded`
whether or not it already had attempts.

#### Scenario: A requeued job that never runs again
- **WHEN** a job whose first attempt failed on infrastructure stays queued past the bound
- **THEN** it is `expired` with cause `queue_age_exceeded` carrying its attempt count and last placement blockers

### Requirement: The bounds are visible before they bite

The capabilities response SHALL state the bounds and exceptions; a running job's view SHALL carry
`wall_clock {limit_seconds, source, elapsed_seconds, remaining_seconds}`; the roster SHALL warn
`attempt_near_wall_clock_bound` and `queue_near_age_bound` at 80% of a bound.

#### Scenario: Bounds and approaching deadlines are visible
- **WHEN** a caller reads capabilities, a running job view, and the roster for a job approaching its configured bound
- **THEN** capabilities state the authority-wide bounds and any named per-handler exceptions
- **AND** the running job view carries its limit, source, elapsed time, and remaining time
- **AND** the roster emits the corresponding near-bound warning when the attempt or queue age reaches 80% of its limit
