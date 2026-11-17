## ADDED Requirements

### Requirement: Operation profiles and delivery intent influence feasible placement
Workloads SHALL declare bounded resource profiles and optional result-delivery intent. Placement SHALL keep resource samples scoped to comparable profiles and account for delivering a usable result among eligible workers. Missing evidence SHALL remain unknown. Preferences SHALL NOT override admission, physical-host policy or execution limits.

#### Scenario: Large output favors a feasible local producer
- **WHEN** a local candidate fits and its usable-result estimate is better after transfer cost
- **THEN** it is preferred with attributable decision evidence

#### Scenario: Local capacity cannot serve the job
- **WHEN** the local host is busy, excluded or lacks required resources
- **THEN** placement considers other eligible candidates without relaxing requirements

#### Scenario: Operations share a handler
- **WHEN** two build operations have different resource profiles
- **THEN** observations from one do not teach the other's admission claim
