## ADDED Requirements

### Requirement: Image pre-warm is driven by a declared manifest, not host lists
A warm job SHALL be built from the app's schema-validated image-warm manifest and run only on an attempt whose Docker data root is persistent; any other attempt SHALL finish `skipped` with its reason.

#### Scenario: Worker without docker_cache
- **WHEN** a warm job is claimed by a worker with no persistent Docker cache
- **THEN** it finishes successfully as `skipped` without building

#### Scenario: Unchanged inputs
- **WHEN** the published image fingerprint equals the last warmed fingerprint
- **THEN** no warm job is submitted

#### Scenario: Queued work
- **WHEN** the app has queued jobs
- **THEN** no warm job is submitted
