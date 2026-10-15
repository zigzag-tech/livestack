## ADDED Requirements

### Requirement: Extraction uses the existing qualified nominal 27B profile

The worker SHALL accept a single `ask` extraction question under the existing
pane-ask profile. It SHALL execute only with recorded qualification or an explicit
internal-testing operator waiver bound to the profile, unit, model revision and
tokenizer hash, and the
nominal [26,28) billion-parameter tier. It SHALL NOT use retired Laya adapters,
small models or cloud fallback. Bodies SHALL be bounded to 64 KiB and generation
to 1024 tokens with thinking and tools disabled.

#### Scenario: Profile is unqualified
- **WHEN** the extraction profile lacks qualification and a matching operator waiver
- **THEN** admission returns a named profile-unavailable result before GPU work

#### Scenario: Owner waives the quality release gate
- **WHEN** private runtime configuration supplies a digest-bound owner waiver for the exact engine identity
- **THEN** the worker may serve best-effort extraction with an `operator-waiver:sha256:` execution marker
- **AND** the worker does not claim successful calibration or model-quality qualification
- **AND** authentication, fleet admission, grounding and bounded execution still apply

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
