## ADDED Requirements

### Requirement: Handler priority classes

The authority SHALL accept `limits.handler_priority`, a map of at most 64 handler ids to integers in [0, 1000000]. A queued job's effective priority SHALL be the greater of its own `priority` and its handler's declared class. Non-starved queued jobs SHALL be considered in descending effective priority, then age. Jobs older than `starvation_seconds` SHALL continue to be considered first. An absent map SHALL leave placement unchanged.

#### Scenario: A release handler outranks the e2e queue
- **WHEN** a job of a handler declared at 1000 and a job of an undeclared handler are both queued
- **THEN** the declared handler's job is placed first when both fit

### Requirement: A waiting higher-priority job holds host resources against lower priority

When a queued job is refused only for lack of free resources (CPU, memory, disk) on a host whose capacity could ever satisfy it, placement SHALL, for the rest of that round, charge the job's admission to that host against every job of strictly lower effective priority. A lower job that fits in what remains SHALL still be placed. Jobs of equal priority SHALL NOT be bound. Running attempts SHALL NOT be preempted. A lower job refused solely because of such a charge SHALL carry blocker code `held_for_higher_priority` naming the holding job.

#### Scenario: Room freed by a finishing attempt goes to the waiting release build
- **WHEN** a release job needing 16 GiB waits on a host with 8 GiB free and an e2e job needing 8 GiB is queued
- **THEN** the e2e job is not placed, its blocker is `held_for_higher_priority`, and after 16 GiB frees the release job is placed

#### Scenario: A lower job needing a different dimension is unaffected
- **WHEN** the release job waits only for CPU and a lower job asks only for memory that is free
- **THEN** the lower job is placed

#### Scenario: Aging still applies
- **WHEN** a lower job has waited past `starvation_seconds`
- **THEN** it is considered before higher-priority jobs and holds what it lacks
