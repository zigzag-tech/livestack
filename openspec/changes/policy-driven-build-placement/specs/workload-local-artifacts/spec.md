## Purpose

Deliver verified results to a consumer on the producing physical host without requiring a remote binary round trip.

## ADDED Requirements

### Requirement: Local artifact acceptance is bound and bounded
A same-host artifact SHALL be bound to its authenticated owner, source, job, attempt/fence, physical host, digest, size and expiry. Local storage SHALL enforce byte, count and age bounds. A caller path SHALL NOT grant access. Expiry or cleanup SHALL never make an unverified result acceptable.

#### Scenario: Authorized local consumer accepts a result
- **WHEN** the producing receipt and local bytes match the consumer's requested immutable result
- **THEN** the consumer accepts without uploading and downloading those result bytes

#### Scenario: A handle is stale, foreign or tampered
- **WHEN** its authority binding, lease or local content fails verification
- **THEN** handoff fails with a named reason and no unverified bytes are accepted

### Requirement: Explicit read handoffs preserve separate credentials
A producer SHALL explicitly grant a registered recipient expiring read access to selected source objects or a succeeded job. A read grant SHALL NOT confer ownership, mutation or re-sharing rights. Grants SHALL be bounded globally to 4096 job and 16384 object records, with at most 128 objects per request and a maximum 24-hour lifetime.

#### Scenario: Coordinator and consumer use distinct credentials
- **WHEN** source or completed results are handed between their authenticated principals
- **THEN** the recipient reads only explicitly granted content until expiry and cannot cancel or re-share the producer's job
