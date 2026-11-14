## ADDED Requirements

### Requirement: A handler has one default per platform

The registry SHALL keep at most one default release per handler id and platform/architecture. Activating a release
whose manifest platform/architecture differ from the handler's primary default SHALL set that platform's default and
SHALL NOT change the primary. A default-selection job SHALL be accepted with the primary and every platform default of
the same generation, and SHALL keep that set for every attempt and retry. Placement SHALL assign it only to a worker
advertising one of those exact releases and SHALL pin the attempt to the release that worker advertises.

#### Scenario: An aarch64 release is activated beside an x86_64 default
- **WHEN** the default for a handler is an x86_64 release and an operator activates an aarch64 release of it
- **THEN** the x86_64 release stays the primary default and the aarch64 release becomes that platform's default

#### Scenario: One job runs on either platform
- **WHEN** a default-selection job is accepted while both platform defaults exist
- **THEN** an x86_64 worker's attempt is pinned to the x86_64 release and an aarch64 worker's attempt to the aarch64 release, and each result is checked against its attempt's release

#### Scenario: A platform default changes after acceptance
- **WHEN** a platform default changes after a job was accepted
- **THEN** the job's attempts and retries use the releases it was accepted with
