## MODIFIED Requirements

### Requirement: Requests with no warm local option are ranked over the fleet
When the entry node has no resident, unsaturated unit that satisfies a request within its
allowed regions, it SHALL rank the request over its regional view before admitting a
local load or queueing, whether or not the request states a preference. A resident unit
with no free slot is saturated, and the routing record SHALL name the trigger
`saturated`. A local load or a local queue SHALL win only when the ranking chooses this
node.

#### Scenario: Warm in-region peer beats a local load
- **WHEN** an indifferent request enters a node with nothing resident and a warm,
  satisfying node exists in the same region
- **THEN** the request is routed `warm` to that node and no local load starts

#### Scenario: A saturated resident unit asks the fleet
- **WHEN** an indifferent request enters a node whose only satisfying resident unit has
  no free slot
- **THEN** the request is ranked with trigger `saturated`, and it queues locally only if
  the ranking chooses this node

## ADDED Requirements

### Requirement: A saturated warm target is priced by finish time
When any warm candidate in a ranking has no free slot, every candidate SHALL be priced by
its finish time (queue wait, measured load when cold, first token, expected tokens over
decode rate) and ordered by it, whether or not the request states a speed preference. A
cold candidate SHALL join the warm ones only when it finishes sooner than every warm one.
When any warm candidate is unpriced, the warm-first rule SHALL stand.

#### Scenario: An idle cold copy beats a long queue
- **WHEN** the only warm unit is full with a 45 s estimated wait and the same unit is
  unloaded on another node with a measured 4 s load
- **THEN** a one-token request is routed `cold` to that node

#### Scenario: A short queue keeps its traffic
- **WHEN** the only warm unit is full with a 1.5 s estimated wait and a cold copy needs a
  4 s load
- **THEN** the request stays on the warm unit
