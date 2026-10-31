## Purpose

Provides a bounded owner-backed link from an Overlord turn to the workload job and exact execution attempt it caused, including a safe current explanation for graph consumers.

## ADDED Requirements

### Requirement: Workload jobs preserve an exact source turn claim

An authenticated workload submitter MAY include `source: {kind: "overlord_turn", turn_id}` only when its operator-configured principal has the source-turn capability. Other principals SHALL be refused before job admission. The workload authority SHALL validate the closed object shape and bounded stable turn id, derive the job owner only from the authenticated principal, and persist the source claim immutably with the job. The source id SHALL NOT be taken from labels or used to select the owner. Owner-bound job reads SHALL return the exact source id and MAY filter by exact source turn id. Consumers SHALL match the returned id byte-for-byte against their authenticated turn id; missing, malformed, mismatched or incomplete source data SHALL remain unknown.

#### Scenario: A job is linked to its exact authenticated turn
- **WHEN** an authorized submitter creates a job with a valid Overlord turn source
- **THEN** the authority stores that source id with the job and the fixed-owner reader returns the same id
- **AND** the job remains owned by the authenticated principal regardless of the source object

#### Scenario: A principal without source-turn authority submits a claim
- **WHEN** a principal without the operator-configured source-turn capability submits a turn source
- **THEN** the authority refuses the request before creating a job
- **AND** the principal's authenticated owner remains unchanged

#### Scenario: A legacy job has no source turn
- **WHEN** a job is submitted without source lineage
- **THEN** the owner reader reports no source id for that job
- **AND** consumers do not infer a link from labels, handler, capability, time or job order

#### Scenario: A repeated intent conflicts on source identity
- **WHEN** an owner replays an existing intent id with a different source turn id
- **THEN** the authority refuses the conflict without changing the existing job or its source

### Requirement: Owner reads expose the exact worker session and a safe current explanation

The fixed-owner job source SHALL return the authority-issued job and attempt ids, worker id, and the worker session id from the claimed attempt's authenticated boot identity. It SHALL return a bounded current explanation containing an authority-derived state code and observation time. Explanation codes SHALL come from a closed set; the response SHALL NOT include free-form handler progress text, command logs, artifact contents, credentials or transcript data. Missing attempt or explanation data SHALL remain absent and SHALL NOT be reconstructed from another job or worker.

#### Scenario: A running attempt identifies its exact worker session
- **WHEN** an owner reads a job with a live attempt
- **THEN** the response identifies that attempt, its worker and the boot session authenticated for that attempt
- **AND** the session identity changes when a later retry is claimed by a different boot session

#### Scenario: The authority returns a current explanation without worker prose
- **WHEN** an owner reads a queued, running or terminal job
- **THEN** the response includes the latest authority-derived explanation code and observation time when available
- **AND** it omits arbitrary progress messages, logs, artifact bodies and credentials

#### Scenario: A worker session or explanation is missing
- **WHEN** the job has no attempt or the authority cannot derive a current explanation
- **THEN** the corresponding relation remains absent or unknown
- **AND** the reader does not select a nearby attempt, worker or explanation

### Requirement: Exact-turn workload reads remain owner-scoped and bounded

The job-reader route SHALL accept an exact, bounded source turn id and return only rows for both its authenticated fixed owner and that exact id. The query SHALL be indexed and bounded independently of the owner's history; responses SHALL remain within the existing 256-row and 30-KiB limits. A complete empty result SHALL be distinguishable from a truncated result, and a consumer SHALL treat a truncated page as incomplete rather than a complete turn join.

#### Scenario: Exact-turn lookup returns only matching owner jobs
- **WHEN** a fixed-owner reader requests one turn id
- **THEN** every returned job has the exact same source turn id and the authenticated owner
- **AND** a job from another owner or another turn is not returned

#### Scenario: No job is linked to the exact turn
- **WHEN** a valid exact-turn lookup has no matching workload job
- **THEN** the authority returns a complete empty result
- **AND** the consumer preserves an unknown relation instead of choosing another job

#### Scenario: The exact-turn result exceeds its bound
- **WHEN** more matching jobs exist than fit the bounded result
- **THEN** the authority marks the page incomplete with its bounded continuation information
- **AND** the consumer does not treat that page as the full workload lineage

## MODIFIED Requirements
