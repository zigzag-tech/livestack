## ADDED Requirements

### Requirement: Extraction uses the existing qualified nominal 27B profile

The worker SHALL accept a single `ask` extraction question under the existing
pane-ask profile. It SHALL execute only with recorded qualification and the
nominal [26,28) billion-parameter tier. It SHALL NOT use retired Laya adapters,
small models or cloud fallback. Bodies SHALL be bounded to 64 KiB and generation
to 1024 tokens with thinking and tools disabled.

#### Scenario: Profile is unqualified
- **WHEN** the extraction profile lacks qualification
- **THEN** admission returns a named profile-unavailable result before GPU work

#### Scenario: Extraction succeeds
- **WHEN** the qualified worker returns a valid source-grounded extraction
- **THEN** the response echoes request, evidence and profile identities exactly
- **AND** its execution metadata names the model revision and qualification

#### Scenario: No question is present
- **WHEN** qualified extraction finds no user-directed question
- **THEN** it returns successful `no_grounded_ask`, distinguishable from failure

#### Scenario: Invalid output or unavailable model
- **WHEN** output violates the extraction contract or inference fails
- **THEN** the worker returns a named typed failure and no invented question
