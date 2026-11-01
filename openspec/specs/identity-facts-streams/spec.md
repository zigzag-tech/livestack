# identity-facts-streams Specification

## Purpose
Expose Livestack-owned host identity observations through bounded native streams so each owning authority remains the single writer of its complete `hosted_on` Fact cut.

## Requirements

### Requirement: Identity Facts are published by their owning authenticated source

The fleet broker SHALL publish only Harmony node Facts through `livestack.identity-facts.fleet/1`. The workload authority SHALL publish only worker Facts through `livestack.identity-facts.workload/1`. Each state contract SHALL be account-scoped, host-service, and one-per-realm. A producer SHALL register through the authenticated Benchday daemon ingress and Hub authority path before publishing; a local publisher without the authority link SHALL NOT be treated as serving. The fleet contract SHALL be published only by the observe-only fleet broker. Each source SHALL use its explicitly configured Livestack authority id and SHALL NOT infer one from a hostname.

#### Scenario: A configured fleet authority publishes

- **WHEN** the observe-only fleet broker has an explicit authority id and authenticated stream authority registration
- **THEN** it publishes only `harmony:node:` Facts through the fleet identity contract

#### Scenario: A host broker attempts fleet publication

- **WHEN** an ordinary host broker starts the stream runtime
- **THEN** it does not register or publish the one-per-realm fleet identity contract

#### Scenario: The authority link is absent or refused

- **WHEN** a source cannot register through the authenticated Hub authority path or another live holder owns the contract
- **THEN** publication remains unavailable with a named outcome and no partial cut is accepted

### Requirement: Every publication is one bounded complete cut

Each refresh SHALL mint one source generation/sequence/observation-time tuple and divide its full Fact v1 snapshot into one to 128 stable-keyed pages. All pages SHALL carry matching cut metadata and zero-based contiguous indices. The complete canonical snapshot SHALL contain at most 4,096 Facts and at most 2 MiB; each complete page partial SHALL be at most 24 KiB. An empty cut SHALL be represented by exactly one page with no Facts. The producer SHALL validate all bounds before publishing the first page and SHALL NOT publish a partial candidate after validation failure.

#### Scenario: The source publishes a complete multi-page cut

- **WHEN** a valid source snapshot requires more than one page
- **THEN** every emitted page carries the same source fence and the pages cover the complete Fact set in index order

#### Scenario: A cut exceeds a bound

- **WHEN** the Fact count, canonical bytes, complete partial bytes, or page count exceeds its limit
- **THEN** the publisher reports the named bound failure and sends no page of that candidate cut

#### Scenario: A source cut becomes empty or shorter

- **WHEN** a newer complete cut contains no Facts or fewer pages than its predecessor
- **THEN** the source publishes one empty page or publishes only indexes below the new page count
- **AND** the Hub ignores any trailing page partials with the old generation/sequence and lets them expire under the stream TTL

### Requirement: Source freshness and failure do not become absence

Each source SHALL refresh its complete cut no less often than every 15 seconds while running, against the stream's 60-second TTL and Fact freshness. A failed read, disconnected ingress, registration refusal, or partial send SHALL be logged as a named failure; it SHALL NOT be represented as a successful empty cut. The producer SHALL retry a fresh complete cut and stop its refresh loop when the runtime stops. These identity reads SHALL NOT create placement or decision-ledger records.

#### Scenario: Ingress drops during a page set

- **WHEN** publishing one page fails after an earlier page was acknowledged
- **THEN** the source records the failure and retries a newer complete cut; it does not claim that the incomplete page set was complete

#### Scenario: Identity source stops

- **WHEN** the workload or fleet stream runtime stops
- **THEN** its periodic identity refresh task is cancelled and the producer closes through the normal lifecycle
