## ADDED Requirements

### Requirement: Engine source activation is a validated operator transaction
The operator SHALL have a read-only plan and explicit apply command that validates the full committed source revision, matching artifact bytes and interpreter before changing a unit. Apply SHALL acquire the existing exclusive-device lease, compare the complete declaration digest, preserve unrelated units, atomically update source selection and pin together, and retain a bounded private receipt. It SHALL NOT force model residency, cancel reservations or disguise a new source as an old revision.

#### Scenario: Candidate is stale or modified
- **WHEN** source, artifact bytes, expected pin or declaration digest differs from the validated plan
- **THEN** apply refuses before changing the live declaration

#### Scenario: Active reservation blocks activation
- **WHEN** Harmony cannot grant the operator an exclusive-device lease within the declared window
- **THEN** no source selection, declaration or service is changed

#### Scenario: Restart fails after selection
- **WHEN** the selected facade fails its bounded restart
- **THEN** the command restores the exact previous declaration only while its candidate digest still owns the file, attempts the previous service restart, and records failure or uncertain rollback distinctly
