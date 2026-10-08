## ADDED Requirements

### Requirement: Attempt resource usage is measured and survives a kill

The worker SHALL sample the attempt's cgroup memory peak, OOM and task-limit events, CPU
usage and workspace disk delta while the attempt runs, and SHALL merge the last good
sample into the attempt result when execution ends without an exit receipt. The result
SHALL state whether each figure came from the receipt or a sample, and SHALL state
`resource_evidence: "none"` when neither exists. An unavailable measurement SHALL NOT be
reported as zero.

#### Scenario: The supervisor is killed before it writes a receipt
- **WHEN** the kernel OOM-kills an attempt's processes and the wrapper exits without a receipt
- **THEN** the attempt result carries the sampled `oom_kill` count and `source` "sampled"

#### Scenario: No cgroup evidence exists
- **WHEN** an attempt ends without a receipt and no cgroup file could be read
- **THEN** the result says `resource_evidence` "none" and no resource figure is zero-filled

### Requirement: Resource-limit kills are a typed terminal cause

An attempt that ended with an OOM kill, a task-limit event or workspace exhaustion SHALL
complete as failed with cause `resource_limit`, naming the dimension, the observed figure
and the declared need, and SHALL NOT be retried unchanged. `execution lease lost` and
`execution stopped without a result` SHALL be reported only when no resource evidence
shows a limit.

#### Scenario: A compile exceeds its declared memory
- **WHEN** a handler declared 8 GiB and its attempt is OOM-killed at 8.2 GB
- **THEN** the job fails with `resource_limit` kind `memory`, the observed 8.2 GB and declared 8 GiB, and is not retried

### Requirement: The authority keeps bounded per-handler resource history

The authority SHALL keep, per handler and dimension, a rolling window of at most a
configured number of recent succeeded and resource-limited attempts (and at most a
configured age), and SHALL serve count, p50, p95 and maximum. History SHALL be derived
data: its loss SHALL show as "no history", never as a figure. Computing status SHALL
issue a number of database statements independent of the number of handlers.

#### Scenario: History is bounded
- **WHEN** ten times the window of attempts finish for one handler
- **THEN** at most the window remains and the statistics reflect the newest

### Requirement: Declarations below observed need are flagged

A handler whose declared enforced need is below the observed p95 or any observed peak, with
at least the configured minimum samples, SHALL be flagged `declared_below_observed` with
the figures in handler status and the roster. Fewer samples SHALL NOT raise a flag.

#### Scenario: Declared 8 GiB, observed max 8.17 GB over twelve attempts
- **WHEN** status is read
- **THEN** the handler is flagged with need, p95, max and sample count

### Requirement: An admission floor is optional and fails closed

When the authority configuration enables `resource_floor`, a submit whose need is below
`max(observed maximum, p95) x margin` for a covered handler SHALL be refused with the
floor, the margin and the evidence named. The section SHALL be schema validated with
unknown keys rejected at startup and reload without echoing values. Insufficient history
SHALL NOT raise a floor. When the section is absent no floor SHALL apply.

#### Scenario: Submit just below and just above the floor
- **WHEN** need is one byte below the floor and then equal to it
- **THEN** the first is refused with the named floor and the second is accepted

#### Scenario: History unreadable
- **WHEN** the history cannot be read and `strict` is false
- **THEN** no floor applies, status reports `resource_floor_unavailable`, and a strict policy refuses by name

### Requirement: Reported metrics are defined and instruments are verified

Every timing or number in a result's metrics SHALL be declared with a name, unit, what it
measures and what it excludes, in the worker's schema or the handler release manifest.
Undeclared names SHALL be omitted from status surfaces and counted visibly. Each new
instrument SHALL ship with a test in which the measured quantity moves by a known amount,
and a missing measurement SHALL be distinguishable from zero in that test.

#### Scenario: A mis-scoped metric
- **WHEN** a handler reports a metric declared as a sub-phase whose value exceeds its enclosing whole
- **THEN** the schema test fails and the value is not shown as a cost
