## ADDED Requirements

### Requirement: Host status exposes bounded unit outcomes and load summaries

The host broker's `/status` response SHALL include a `counters` object with
60 one-minute buckets (a 60-minute window) per tracked unit kind for
`admitted`, `completed`, `failed`, and `evicted`. Each kind's counters SHALL
include UTC epoch-second bucket starts and the time from which that process has
observed the kind, so a restarted broker SHALL NOT present lost history as
zero.
Each bucket count SHALL be at most `2^53 - 1`; a saturated count SHALL carry an
explicit marker rather than wrapping. Omitted minute buckets after
`observed_from_s` SHALL mean zero; buckets before it SHALL mean unknown.

`admitted` SHALL count a placement grant returned for a caller request.
`completed` and `failed` SHALL count only an explicit caller-reported outcome
on lease release. An expired lease or release without an outcome SHALL remain
unknown and SHALL NOT increment either counter. `evicted` SHALL count only an
eviction successfully dispatched to its owning node.

The `counters` object SHALL also expose, per unit kind, `queue_depth` when the
node reports a valid non-negative waiting count, and `p50_ms` / `p95_ms` when
the broker has a valid finite caller-reported duration from zero through 365
days. It SHALL retain at most 256 latency samples per unit kind and SHALL
report the sample count. A missing
or invalid queue value or an empty latency sample SHALL be omitted, never
rendered as zero. The broker SHALL track at most 64 unit kinds and SHALL report
whether additional kinds were omitted.

All telemetry SHALL be in memory, bounded independently of request volume,
and reset on broker restart. Status collection SHALL reuse the peer snapshots
already taken for the response; it SHALL NOT add a peer request per unit.
Telemetry SHALL NOT replace or duplicate the broker's decision ledger.

#### Scenario: Reported job outcomes create bucket counts and latency
- **WHEN** a caller receives an admitted placement and later releases its lease with status `ok` and a valid wall duration
- **THEN** the unit kind's current bucket increments `admitted` and `completed`
- **AND** the duration contributes to its bounded latency sample and p50/p95
- **AND** the existing grant and outcome ledger records remain the only decision records

#### Scenario: Unknown outcomes stay unknown
- **WHEN** a lease expires or is released without an explicit `ok` or `failed` status
- **THEN** neither `completed` nor `failed` is incremented for that lease
- **AND** the missing outcome is not represented as a zero-latency sample

#### Scenario: Failed caller outcome is counted
- **WHEN** a caller releases a lease with status `failed`
- **THEN** the unit kind's current `failed` bucket increments
- **AND** a valid reported wall duration contributes to latency percentiles

#### Scenario: An unbounded duration is refused
- **WHEN** a lease release reports a non-finite duration or a duration greater than 365 days
- **THEN** the release request is refused with HTTP 422 and the lease remains available for a valid release

#### Scenario: Queue state is reported only when known
- **WHEN** a peer snapshot includes a valid queue waiting count for a unit
- **THEN** `/status.counters` includes that unit's summed `queue_depth`, including a real zero
- **WHEN** the snapshot omits or malforms the queue waiting count
- **THEN** `/status.counters` omits that unit's `queue_depth`

#### Scenario: A failed peer refresh does not produce a partial total
- **WHEN** any peer refresh fails while `/status` gathers queue snapshots
- **THEN** `queue_depth_complete` is false
- **AND** all queue-depth gauges are omitted from that response
- **AND** the peer error remains visible in the existing `peers` response

#### Scenario: Eviction counts only after dispatch succeeds
- **WHEN** the planner proposes an eviction and the owning node accepts the eviction request
- **THEN** the unit kind's `evicted` bucket increments
- **WHEN** the dispatch fails or is disabled
- **THEN** the bucket does not increment

#### Scenario: Storage stays bounded across sustained traffic
- **WHEN** more than 60 minutes of outcomes and more than 256 latency samples per unit arrive
- **THEN** only the current 60 one-minute counter buckets and at most 256 latency samples per unit remain in memory
- **AND** the status response tracks at most 64 unit kinds and reports whether kinds were omitted
