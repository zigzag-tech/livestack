## ADDED Requirements

### Requirement: Storage bounds derive from filesystem headroom

Each authority and worker store SHALL have an effective byte bound equal to the minimum of
its absolute cap and, when configured, a fraction of its filesystem's capacity, and SHALL
refuse new bytes that would leave less than a configured free-space floor. The effective
bound, its inputs and its state SHALL be computed in one place, logged at startup, on
reload and on change, and served in status. A filesystem whose free space cannot be read
SHALL refuse admission above a small allowance by name and SHALL NOT be assumed to have room.

#### Scenario: Quota below the cap but headroom exhausted
- **WHEN** the store is under its absolute cap and the filesystem has less free than the floor plus the object size
- **THEN** the put is refused with `storage_headroom`, the free space and the floor named, and no bytes are written

#### Scenario: Exactly at the floor
- **WHEN** free space after the put would equal the floor, and then one byte less
- **THEN** the first is admitted and the second refused

#### Scenario: Free space unreadable
- **WHEN** the filesystem statistics cannot be read
- **THEN** a large put is refused as `storage_headroom_unknown`

### Requirement: Low headroom triggers one bounded GC pass before refusal

Before refusing for headroom the authority SHALL run at most one bounded garbage-collection
pass per refresh window that removes only unreferenced objects and expired references, oldest
first, SHALL re-read free space, and SHALL report what it freed in the refusal. It SHALL NOT
delete a referenced object to make room.

#### Scenario: GC makes room
- **WHEN** unreferenced aged objects exceed the deficit
- **THEN** the put succeeds after the pass

#### Scenario: Only referenced bytes remain
- **WHEN** nothing deletable remains
- **THEN** the refusal says all remaining bytes are referenced and names the largest owners

#### Scenario: Refusal storm
- **WHEN** many puts fail within one refresh window
- **THEN** garbage collection runs once

### Requirement: Retention is tiered and never unbounded by default

The authority SHALL support retention windows per terminal job outcome and, per reference
owner and prefix, a time-to-live together with a number of newest references to keep. A
reference rule SHALL NOT express "never expires" without an explicit acknowledgement.
References matched by no rule SHALL be reported with their count and bytes as unbounded. A
dry-run endpoint SHALL report what a retention pass would delete without deleting.

#### Scenario: Failed jobs outlive succeeded jobs
- **WHEN** a failed and a succeeded job are each older than the succeeded window and younger than the failed window
- **THEN** only the succeeded job is removed

#### Scenario: Release references expire
- **WHEN** a release-reference rule keeps the newest ten and a fourteen-day TTL
- **THEN** references beyond the ten newest and older than the TTL are removed and the rest retained

#### Scenario: Unmatched owner
- **WHEN** references exist for an owner with no rule
- **THEN** status lists them as unbounded with count and bytes

### Requirement: Disk refusals are named and announced

A worker whose offered disk is reduced by a reserve SHALL report the filesystem, free
space, reserve and offered figures. Placement SHALL name a disk-driven refusal with those
figures instead of a generic shared-resource reason. A job that waits longer than a
configured period for the same reason on every candidate worker SHALL raise one bounded
event.

#### Scenario: Reserve exceeds free space
- **WHEN** a worker's reserve is 64 GiB and its filesystem has 60 GiB free
- **THEN** its report shows zero offered disk with `reserve_exceeds_free`, the roster flags it, and a waiting job names the figures

### Requirement: Free-space state is a status surface

Authority and worker status and the roster SHALL show filesystem free bytes and fraction,
the effective bound, the floor and a state of ok, low or refusing, and a state change SHALL
leave one bounded event.

#### Scenario: Crossing the alert threshold
- **WHEN** free fraction falls below the alert fraction
- **THEN** state becomes low and exactly one event is recorded

### Requirement: CPU admission uses a verified signal

CPU admission policies SHALL include one based on `some` pressure over a short window and one
based on the runnable queue per core. At startup and reload the worker SHALL verify the
chosen signal with a bounded synthetic CPU load; a signal that does not move SHALL be
reported inert, the worker SHALL report no CPU available with the policy named, and it SHALL
NOT silently fall back to another policy. Placement refusal reasons SHALL carry a stable
code and figures in status.

#### Scenario: Signal moves under load
- **WHEN** the self-test runs more busy workers than cores
- **THEN** the signal rises above its idle reading and the policy is active

#### Scenario: Signal frozen at zero
- **WHEN** the pressure file reads zero under the synthetic load
- **THEN** the policy is inert, CPU is reported unavailable with that reason, and no other policy is substituted

#### Scenario: Load average above cores
- **WHEN** load average exceeds cores but the runnable queue and `some` pressure are low
- **THEN** a worker on the verified policy still offers its CPU
