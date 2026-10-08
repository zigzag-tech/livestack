## ADDED Requirements

### Requirement: Handler activation on a rolled set passes a canary first

When a worker set has a declared rollout spec in `enforce` mode, a handler bundle that differs from what the set serves SHALL be activated on one canary worker and pass its smoke probes before it is activated on any other worker of the set. Activation by explicit pin on a worker outside any rollout spec SHALL behave as before.

#### Scenario: A bundle with a syntax error never leaves the canary
- **WHEN** a new bundle's `handler_import` smoke probe fails on the canary
- **THEN** no other worker of the set is offered the bundle, the canary returns to its previous digests, and the unit is recorded as rejected naming `handler_import`

#### Scenario: A worker outside any rollout spec
- **WHEN** a worker belongs to no declared set
- **THEN** registry activation behaves exactly as it did before this change
