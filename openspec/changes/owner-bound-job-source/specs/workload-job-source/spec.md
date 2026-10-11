## Purpose

Provides a least-privilege, owner-bound way for a colocated consumer to read bounded incremental workload changes without access to other owners' jobs or workload administration.

## ADDED Requirements

### Requirement: Job source credentials are fixed-owner and read-only

The authority SHALL accept a dedicated `job_reader` principal whose identity is one fixed job owner. A `job_reader` credential SHALL be authorized only for the owner-scoped incremental job-change read route and SHALL NOT inherit caller, worker, or admin permissions.

#### Scenario: Reader can access only its owner-scoped job changes
- **WHEN** a valid `job_reader` credential requests incremental job changes
- **THEN** the authority returns only jobs whose effective owner equals the principal id and identifies that id in response metadata

#### Scenario: Reader cannot select another owner
- **WHEN** a `job_reader` request includes an `owner` query parameter
- **THEN** the authority rejects the request and returns no job data

#### Scenario: Reader cannot perform workload or administrative operations
- **WHEN** a `job_reader` credential requests any route other than incremental job changes
- **THEN** the authority refuses the request

### Requirement: Incremental job changes remain bounded and resumable

The authority SHALL expose an authenticated incremental job-change read route that derives the owner from the `job_reader` credential, accepts a bounded watermark and optional tie-safe continuation cursor, and returns no more than 256 rows and 30 KiB per response. The route SHALL reject unknown or repeated query parameters and SHALL NOT expose arbitrary owner selection.

#### Scenario: Polling returns one bounded owner projection
- **WHEN** a reader requests changes after an `updated_since` watermark
- **THEN** the authority returns only that reader's effective-owner rows, each with authority-derived state and outcome, within both response bounds

#### Scenario: Pagination resumes timestamp ties without gaps
- **WHEN** more changes remain at a page boundary
- **THEN** the response supplies a continuation cursor ordered by update timestamp and job id, and resuming from that cursor does not skip or duplicate rows within the paging pass

#### Scenario: Later polling replays its timestamp boundary
- **WHEN** a new polling pass starts from its last timestamp without a continuation cursor
- **THEN** the authority includes changes at that boundary so concurrent timestamp ties remain observable

#### Scenario: Invalid or oversized request is refused
- **WHEN** a request supplies malformed, repeated, unknown, or out-of-range query values
- **THEN** the authority returns a named client error without performing an unbounded query
