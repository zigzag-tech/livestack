# workload-upload-grants Specification

## Purpose
Allows an authorized workload owner to delegate one exact immutable object upload to a separate publisher without sharing job submission or object-read authority.

## Requirements

### Requirement: A workload owner can issue an exact, expiring upload grant

The authority SHALL issue an upload grant only to a caller principal explicitly configured to delegate uploads. A grant SHALL target that caller's own object namespace and bind one request id, SHA-256 digest, exact byte size, and bounded expiry. The authority SHALL reject arbitrary owner selection, changed bindings for a repeated request id, invalid digests or sizes, and expiry outside the configured short window. It SHALL return an opaque high-entropy capability once and SHALL persist only its verifier, binding, state, and bounded audit metadata.

#### Scenario: An authorized owner grants one source upload
- **WHEN** an upload-delegating caller requests a grant for request `r1`, digest `d1`, and size `n1`
- **THEN** the authority returns a capability bound to the caller's object namespace, `r1`, `d1`, `n1`, and its expiry
- **AND** the grant issuance is recorded without logging or persisting the capability value

#### Scenario: A caller attempts to target another owner
- **WHEN** the grant request names an object namespace other than the authenticated caller's own namespace
- **THEN** the authority refuses it before creating a grant or changing object ownership

#### Scenario: A repeated request changes its object binding
- **WHEN** a caller reuses a request id with a different digest or byte size
- **THEN** the authority refuses the changed request and preserves the original grant and object state

### Requirement: A transfer capability authorizes only its bound object upload

The capability SHALL authorize only one `PUT` of its bound digest and exact byte size to the workload owner's object namespace. It SHALL NOT authorize object reads, job submission, job observation, grant issuance, or any other route. The authority SHALL stream and verify the entire object before making it visible, and incomplete, oversized, undersized, or digest-mismatched bytes SHALL leave no usable object or successful receipt.

#### Scenario: A publisher uploads the bound object
- **WHEN** a publisher presents a valid capability and streams exactly the bound object
- **THEN** the verified object is stored in the granting workload owner's CAS namespace
- **AND** the authority returns a receipt containing only the grant id, digest, and byte size

#### Scenario: A publisher tries another object or route
- **WHEN** a publisher uses the capability with a different digest, byte size, `GET`, or job/control route
- **THEN** the authority refuses the operation without exposing object bytes or creating a job

#### Scenario: An upload is incomplete or corrupted
- **WHEN** the stream ends early, exceeds its declared size, or hashes to a different digest
- **THEN** the authority removes partial staging and leaves the grant without a successful receipt

### Requirement: Upload grant retries reconcile through durable metadata

The grant owner SHALL be able to read bounded grant status using its normal caller credential. A matching repeated request SHALL return the durable uploaded receipt when one exists; otherwise it MAY rotate an unused capability without changing the bound request. An upload whose response is lost SHALL be discoverable by request id and SHALL NOT require downloading the object. Expired, revoked, and capacity-refused grants SHALL have explicit outcomes. Grant records SHALL be count- and age-bounded, and every issue, refusal, expiry, replacement, and successful upload SHALL leave a bounded audit record with owner, request id, digest, byte size, and outcome but no capability material.

#### Scenario: The upload succeeds but its response is lost
- **WHEN** a publisher's object upload completes but the acknowledgement is not received
- **THEN** the granting caller reads status and obtains the matching durable receipt without transferring the object again

#### Scenario: A publisher retries before upload completion
- **WHEN** a publisher repeats the same request id, digest, and size before any grant has completed
- **THEN** the authority returns or rotates a capability with the same binding and leaves no ambiguous competing object owner

#### Scenario: A capability expires or capacity is exhausted
- **WHEN** a publisher presents an expired grant or the grant ledger has reached its configured bound
- **THEN** the authority names the expiry or capacity refusal and preserves all completed object receipts
