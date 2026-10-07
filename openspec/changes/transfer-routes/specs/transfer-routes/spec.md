## ADDED Requirements

### Requirement: Object transfer selects among declared routes

Object upload and download SHALL choose among routes described by validated descriptors (kind, endpoint, cost class, priority, constraints). Unknown fields, unknown cost classes or kinds SHALL be refused at configuration time. A route SHALL be ineligible, with a named reason, when its direction, size or region constraint excludes the transfer.

#### Scenario: A constraint excludes a route
- **WHEN** a route allows only `get` and a `put` is requested
- **THEN** the route is not tried and the trail records `ineligible: direction put not allowed`

### Requirement: Cost classes are enforced by policy

A route of cost `expensive` SHALL NOT be used while any eligible non-expensive route has not been tried, SHALL NOT be used at all under policy `never`, and MAY be ranked normally under `allow`.

#### Scenario: A free route works
- **WHEN** a free and an expensive route are configured and the free route succeeds
- **THEN** no request reaches the expensive route

#### Scenario: Last resort
- **WHEN** every free route fails and policy is `last_resort`
- **THEN** the expensive route is tried and the trail shows it after the failures

### Requirement: Transfers fail over mid-transfer and resume at the offset

When a route fails during a transfer the next route SHALL continue from the bytes already moved: downloads at the saved byte offset, uploads at the offset held by the authority. No byte SHALL be stored twice, and the whole object SHALL be digest-verified before it is visible.

#### Scenario: Connection reset mid-upload
- **WHEN** route A resets the connection while uploading chunk 3 of 6
- **THEN** route B's first chunk request starts at the end of chunk 2 and the object commits with a verified digest

#### Scenario: Lost acknowledgement
- **WHEN** a chunk reached the authority but its acknowledgement did not reach the client
- **THEN** the retry is answered 409 with the authority's offset and the client continues after that offset without resending

### Requirement: Route health is observed per route and peer

The system SHALL keep EWMA success, throughput and latency per route and peer, open a circuit after consecutive failures, admit one half-open trial after an exponentially growing interval (capped), close it on success, and forget evidence older than a stale TTL. A route skipped because it reports itself unavailable (for example an exhausted relay budget) SHALL NOT count as a failure.

#### Scenario: Breaker cycle
- **WHEN** a route fails past the threshold, the interval elapses, and the trial fails
- **THEN** the circuit reopens with a doubled interval; a successful trial closes it and resets the interval

### Requirement: Failure is loud and bounded

Every abandoned route SHALL be logged with its reason; when all routes fail the raised error SHALL carry the route trail. Routes per set, trail length, in-flight transfers per route and partial uploads (count and idle age) SHALL be bounded.

#### Scenario: All routes down
- **WHEN** every route fails
- **THEN** the error has `route_trail` naming each route and an `all routes failed` line is logged; no partial download file remains

### Requirement: The resumable upload route is additive

The authority SHALL accept `PUT objects/<digest>/upload` only with a valid `Content-Range` at its staged end, verify an optional per-chunk digest, and commit only after whole-object digest verification. An authority without the route SHALL cause clients to use the single-request PUT.

#### Scenario: Old authority
- **WHEN** the route answers 404 to the offset probe
- **THEN** the client performs one whole-object PUT
