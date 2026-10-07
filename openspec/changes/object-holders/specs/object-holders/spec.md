## ADDED Requirements

### Requirement: The authority catalogs content and holders

The authority SHALL record, for each stored object, its owner, SHA-256 digest, size and the set of holders that have verified it. A holder SHALL be a registered store with an identity, address, declared locality, capacity and retention policy. The authority's own object directory SHALL be the built-in holder `authority`; an object with no other holder SHALL behave exactly as today.

#### Scenario: Existing objects are held by the authority
- **WHEN** the authority starts after upgrade with objects already stored
- **THEN** each object has exactly one holding, `authority`, and `GET objects/<digest>` returns the same bytes as before

#### Scenario: An unknown holder is refused
- **WHEN** a store that is not in the holder registry reports a claim
- **THEN** the authority refuses it and records no holding

### Requirement: A holding exists only after verification

A grant MAY name a holder (default `authority`). The authority SHALL record a holding only after the named holder reports the verified digest and exact size for a grant the authority issued for that digest. A claim with a different size, an unissued digest, or an unverified body SHALL leave no holding and SHALL be recorded in the ledger.

#### Scenario: A publisher stores on a nearby holder
- **WHEN** a publisher holding a grant naming holder `h1` uploads the bound object to `h1`, and `h1` reports the digest and size
- **THEN** the catalog lists `h1` as a holder, and no object bytes were written to the authority holder

#### Scenario: A holder reports a wrong size
- **WHEN** `h1` reports the grant's digest with a different size
- **THEN** no holding is recorded and the refusal is ledgered with both sizes

### Requirement: Holder choice is derived, deterministic and explained

`choose_holder` SHALL be a pure function of the request, catalog, declared locality, measurements and policy. It SHALL exclude holders that are unhealthy, lack free capacity, or are not allowed for the caller's principal; rank the rest by declared locality tier, then fresh measured cost, then configuration order; and return the holder with a reason. Absent locality or measurements SHALL NOT be treated as zero cost; with no information it SHALL return `authority`. No application, host or region name SHALL appear in its code.

#### Scenario: Publisher and holder share a host
- **WHEN** the publisher and holder `h1` declare the same `host_id` and the authority holder is on another host
- **THEN** `h1` is chosen and the reason states that publisher and holder share a host

#### Scenario: Measurements outrank a stale locality tier
- **WHEN** two holders have the same locality tier and fresh measurements differ
- **THEN** the holder with the lower measured cost is chosen and the reason cites the measurement and its age

#### Scenario: No information
- **WHEN** no holder declares locality and no measurements exist
- **THEN** `authority` is chosen and the reason says why

#### Scenario: Principal policy excludes a holder
- **WHEN** the caller's principal is not allowed holder `h1`
- **THEN** `h1` is not chosen even if it is nearest, and the reason records the exclusion

### Requirement: Reads fetch by locator with attempt-bound credentials

For a worker fetching a job input, the authority SHALL return the holders of that digest in cost order for that worker, with a short-lived credential bound to the attempt, fence, digest and holder. A holder SHALL serve the object only for a valid credential. A worker SHALL fail over between holders through the transfer route set, verify the full digest, and receive no object for a different digest.

#### Scenario: A worker fetches from a holder on its own host
- **WHEN** a worker's job has an input held by `h1` on the worker's host and by `authority`
- **THEN** the locator lists `h1` first, the worker fetches from `h1`, and the digest verifies

#### Scenario: First holder fails mid-transfer
- **WHEN** `h1` resets the connection partway through a download
- **THEN** the worker continues from the saved offset on the next holder and the final digest verifies

#### Scenario: A credential for another digest
- **WHEN** a worker presents a credential bound to digest `d1` for digest `d2`
- **THEN** the holder refuses it

### Requirement: Jobs wait, not fail, when inputs are unreachable

At admission the authority SHALL verify that every input digest has at least one healthy holder. If not, the job SHALL stay queued with reason `inputs_unavailable` and the ledger SHALL name the digest and last known holders.

#### Scenario: The only holder is down
- **WHEN** the only holder of an input is unhealthy at admission
- **THEN** the job is not admitted, its reason is `inputs_unavailable`, and no worker attempt is created

### Requirement: Placement prefers the worker nearest the inputs

When a job spec does not set `locality_host`, placement SHALL derive it from the host of the holder with the most input bytes. It SHALL remain a preference: a busy or ineligible preferred host SHALL NOT prevent placement elsewhere.

#### Scenario: Inputs are on the worker's host
- **WHEN** two eligible workers exist and the job's inputs are held on one worker's host
- **THEN** the job is placed on that worker and the ledger records the locality hint and its source holder

#### Scenario: The preferred host is full
- **WHEN** the preferred host cannot admit the job
- **THEN** the job is placed on another eligible host, which fetches through the locator

### Requirement: Retention is owned by the holder

The authority SHALL prune only objects it holds. For other holders it SHALL advertise referenced digests and a `retain_until` request, and SHALL NOT delete holder bytes. A digest referenced by a durable job SHALL NOT be pruned from the authority holder.

#### Scenario: Prune on the authority leaves other holders alone
- **WHEN** the authority prunes an unreferenced digest it holds that `h1` also holds
- **THEN** the authority bytes are removed, the catalog still lists `h1`, and `h1` bytes are untouched

### Requirement: Decisions and holders are observable

Every holder choice, refusal and locality hint SHALL leave a ledger record with its reason. The authority SHALL expose holders, holdings, free capacity, health and the last choice per holder.

#### Scenario: A choice is explained
- **WHEN** a holder is chosen for a grant
- **THEN** a ledger record states the chosen holder, the candidates considered, and the reason
