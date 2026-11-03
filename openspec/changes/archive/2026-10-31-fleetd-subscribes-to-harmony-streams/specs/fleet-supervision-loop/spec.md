## ADDED Requirements

### Requirement: Fleet observations come from an authenticated bounded stream

FleetD SHALL obtain its fleet observation from an authenticated Hub subscription
to `harmony.fleet/1` with account scope. The subscription's authenticated
`account_id` SHALL match an explicitly configured expected account. FleetD SHALL
not request `GET /fleet` or use an HTTP fallback for fleet observations; the
existing planning and operation routes remain the authorities for placement and
effects.

The consumer SHALL hold no more than 256 partials, each no larger than 4,096
serialized bytes, in each committed or staged generation. While a replacement
cut is staged, it MAY retain both generations, bounded to 512 partials and 2 MiB
total; each generation SHALL remain within 1 MiB. It SHALL treat a warm cached
receipt (or a receipt without provenance) as stale until a live sample replaces
it. It SHALL accept an observation for planning only when coverage is complete
for a non-empty expected source set and every reporting partial receipt is at
most 20,000 ms old (or the partial's smaller TTL, if present). The authenticated
Hub envelope identity SHALL be `subject.machine`; `subject.node` is not the
envelope identity for this host adapter. It SHALL preserve the raw host-level
partial bodies and SHALL NOT invent worker rows absent from those bodies.

The observation SHALL expose distinct `unknown`, `stale`, `degraded`, and `gap`
states. FleetD SHALL stop before calling the planning route unless the current
observation is fresh and complete.

#### Scenario: Fresh complete account stream

- **WHEN** the authenticated resident account matches the configured account and a non-empty source set reports complete, fresh `harmony.fleet/1` coverage
- **THEN** FleetD supplies the stream-derived host facts to the supervision loop and may request `POST /fleet/plan`
- **AND** it issues no `GET /fleet` request

#### Scenario: Account identity does not match

- **WHEN** the authenticated resident reports an account ID different from the configured account
- **THEN** FleetD reports a degraded observation and makes no fleet subscription or planning request

#### Scenario: No initial complete sample

- **WHEN** the account-scoped subscription has not delivered a complete sample
- **THEN** FleetD reports `unknown` and does not call `POST /fleet/plan`

#### Scenario: A reporting source sample ages out

- **WHEN** any reporting stream receipt is older than the contract TTL or its smaller partial TTL
- **THEN** FleetD reports `stale` and does not call `POST /fleet/plan`

#### Scenario: Producer reports failure

- **WHEN** the subscription is refused or degraded, a partial is failed, denied, incapable, or partial, or the event consumer fails
- **THEN** FleetD reports `degraded` with the source reason and does not call `POST /fleet/plan`

#### Scenario: Coverage has a gap

- **WHEN** coverage is incomplete, the expected source set is empty or inconsistent, a stream reset occurs, or a bounded input is exceeded
- **THEN** FleetD reports `gap` and does not call `POST /fleet/plan`

#### Scenario: Fresh stream contains host summaries only

- **WHEN** a complete fresh partial contains host-level facts but no worker rows
- **THEN** FleetD preserves those facts and reports no synthetic worker rows
- **AND** placement remains the responsibility of `POST /fleet/plan`
