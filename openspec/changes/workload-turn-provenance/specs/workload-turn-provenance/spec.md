## ADDED Requirements
### Requirement: Source identity is explicitly authorized and authority stamped
The authority SHALL accept optional bounded source-turn provenance only from a caller granted its source kind, SHALL stamp authenticated submitting principal and effective owner, and SHALL reject unknown or authority-owned request fields. Omission SHALL preserve prior submission behavior.
#### Scenario: Unauthorized or forged provenance
- **WHEN** an ungranted principal, worker, or caller outside its delegated owner requests provenance
- **THEN** submission is refused without a job or source binding being created
#### Scenario: Idempotent source conflict
- **WHEN** a request key already names a job from another source turn
- **THEN** the authority refuses the mismatch and never relabels or silently replaces the job
### Requirement: Exact-turn reads preserve owner and resource bounds
The fixed-owner reader SHALL filter by validated source_turn_id with indexed set-based work, preserve cursor semantics and response caps, and derive attempts, workers and explanations only from authority-owned state.
#### Scenario: Sibling owner or turn
- **WHEN** an authenticated reader requests a turn associated only with another owner or another turn is present in the same owner history
- **THEN** those jobs and their attempt identities are not returned
### Requirement: Provenance expires independently of retained job data
Provenance SHALL be at most512 bytes per job and expire after a configured window no greater than7days, default24h; reads SHALL hide expired provenance and the normal sweep SHALL clear it even for retained jobs.
#### Scenario: Retained job after provenance expiry
- **WHEN** the source retention deadline passes for a retained job
- **THEN** source metadata is cleared or hidden while the retained job data remains intact
