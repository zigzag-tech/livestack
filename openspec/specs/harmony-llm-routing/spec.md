# harmony-llm-routing Specification

## Purpose
How Harmony routes an LLM request across hosts and regions: which targets are eligible,
from whose vantage they are ranked, what a routed request may and may not cause at its
target, and how routing behaves when the control plane is far away or unreachable.

## Requirements

### Requirement: Region eligibility precedes readiness
Ranking SHALL remove every target outside the request's allowed regions before it
applies warm-before-cold, the finish-time rule, preferences, distance or load. A target
whose node declares no region SHALL be ineligible under any region restriction. Every
removed target SHALL appear in the ranking record with the clause that removed it.

#### Scenario: Warm forbidden target does not suppress cold allowed target
- **WHEN** a request allowed only in `cn` is ranked over a fleet where a `na` node is warm
  for it and a `cn` node is cold for it
- **THEN** the `cn` node is the chosen target and the `na` node is listed as filtered by
  region

#### Scenario: Unknown region is not allowed
- **WHEN** a request allowed only in `na` is ranked and a satisfying node declares no region
- **THEN** that node is filtered and never chosen

### Requirement: Ranking uses the caller's vantage
Fleet routing SHALL rank distance from the caller's vantage: the entry node's own region
(`region:<r>`) unless the request states a narrower one. It SHALL NOT rank from the fleet
broker's own probes when the broker is in a different region from the entry node.

#### Scenario: Nanjing entry prefers Nanjing
- **WHEN** a `cn` entry node routes a request that a warm `cn` node and a warm `na` node
  both satisfy equally, and the fleet broker it reads runs in `na`
- **THEN** the `cn` node ranks first, and the route record names the vantage used

### Requirement: Region policy comes from the principal
A principal's allowed regions SHALL come from the fleet principal table. A request MAY
narrow them with `harmony_regions`; a request that names a region outside its principal's
allowance SHALL be refused with a 403 naming the region and the allowance. A principal
with no allowance configured SHALL be unrestricted.

#### Scenario: Narrowing is honoured
- **WHEN** a principal allowed `na,cn` sends `harmony_regions: ["cn"]`
- **THEN** only `cn` targets are eligible

#### Scenario: Widening is refused
- **WHEN** a principal allowed `cn` sends `harmony_regions: ["na"]`
- **THEN** the response is 403 naming `na` and the allowance `cn`, and nothing is routed

### Requirement: A request routed to a warm target is served resident-only
A routed request SHALL carry the routing node's id and the expectation it was routed
under (`warm` or `cold`). A receiver of a `warm`-routed request SHALL serve it only from
a unit that is resident, satisfies the request's requirement, and has an execution slot;
the satisfaction check and slot acquisition SHALL be one step, and the unit SHALL NOT be
evicted while the request holds the slot. Otherwise the receiver SHALL answer
`409` with `{"error": "route_stale", "reason": "not_resident" | "unsatisfied" |
"saturated" | "policy", "unit": …}` before sending any body byte. A receiver SHALL NOT
admit, load, swap, or forward a `warm`-routed request, and SHALL NOT route any routed
request again.

#### Scenario: Unit evicted since the view
- **WHEN** a `warm`-routed request reaches a node whose satisfying unit was evicted
- **THEN** it answers 409 `route_stale` / `not_resident` and no load starts on that node

#### Scenario: Saturated target
- **WHEN** a `warm`-routed request reaches a unit at `max_concurrent` with its queue full
- **THEN** it answers 409 `route_stale` / `saturated` without queueing the request

### Requirement: A request routed to a cold target may load through its host broker
A `cold`-routed request (chosen by the finish-time rule) SHALL be admitted by the
receiver's own host broker exactly as a local request is, and SHALL NOT be routed again.

#### Scenario: Cold route loads locally at the target
- **WHEN** a request is routed `cold` to a node with no satisfying unit resident
- **THEN** that node's host broker admits the load and the node serves the request

### Requirement: Stale routes are retried once, never mid-stream
On a `route_stale` refusal, a connection failure before any response byte, or a 429, the
routing node SHALL try the next eligible candidate once, then fail with the refusals of
both attempts. It SHALL NOT retry after any response byte has reached the caller, and a
streaming response SHALL hold its execution slot until it completes or the caller
disconnects, regardless of view age.

#### Scenario: One retry then an explained failure
- **WHEN** the first and second candidates both answer `route_stale`
- **THEN** the caller gets an error carrying both targets and both reasons, and no third
  target is tried

#### Scenario: No replay after streaming began
- **WHEN** a target fails after streaming the first chunk
- **THEN** the caller's stream ends with an error and the request is not sent elsewhere

### Requirement: Routed requests travel as asked
Every forward — fleet-routed or to a same-host peer — SHALL carry the caller's
requirement (`model: "require:…"` and/or `harmony_requires`), preferences, region
restriction, owner and authorization headers unchanged; only the receiver resolves the
requirement to a unit.

#### Scenario: Peer forward keeps the requirement
- **WHEN** a request `require:class=llm,context_len>=131072` is forwarded to a same-host peer
- **THEN** the peer receives the same requirement, not a unit name

### Requirement: The peer shortcut is same-host and resident only
On the request path, a node SHALL defer to a peer without asking a broker only when the
peer is on the same host, has a resident (not loading) unit whose attributes satisfy the
requirement, and answers its health probe. Cross-host deferral SHALL go through fleet
ranking.

#### Scenario: Remote holder is not probed per request
- **WHEN** the only holder of a satisfying unit is on another host
- **THEN** the request path makes no health probe to that host and the decision comes
  from the fleet ranking

### Requirement: Requests with no warm local option are ranked over the fleet
When the entry node has no resident, unsaturated unit that satisfies a request within its
allowed regions, it SHALL rank the request over its regional view before admitting a
local load, whether or not the request states a preference. A local load SHALL win only
when the ranking chooses this node.

#### Scenario: Warm in-region peer beats a local load
- **WHEN** an indifferent request enters a node with nothing resident and a warm,
  satisfying node exists in the same region
- **THEN** the request is routed `warm` to that node and no local load starts

### Requirement: Router mode
A router (a harmony-llm facade with no units) SHALL route every request by the rules above
and serve none itself. It SHALL expose the OpenAI-compatible `chat/completions`,
`completions` and `embeddings` endpoints and the `/harmony/route` dry run, parse the
request language with the same code as the facade, and refuse (404, naming the path) any
other `/v1` endpoint, which only specific nodes serve. An embeddings need SHALL be routed
by the facade's one-embedding-space rule: a need met by embedders of different models is a
400, never a choice by distance.

#### Scenario: Localhost router
- **WHEN** a caller sets `base_url` to a router-mode node on its own machine
- **THEN** its requests are served by the ranked target and the response is the target's,
  byte for byte

### Requirement: A router is never a routing target
A router-mode node SHALL NOT register with any host or fleet broker as a serving node,
and SHALL NOT appear as a target in any fleet view or ranking. It MAY report its own
liveness and version on a local health endpoint.

#### Scenario: Router absent from the fleet view
- **WHEN** a router-mode node is running on a requester machine
- **THEN** no fleet broker's `/fleet` view lists it, and no ranking chooses it

### Requirement: A router holds no fleet credential
A router-mode node SHALL forward the caller's `Authorization` and owner headers unchanged
and SHALL NOT attach a fleet or delegating token of its own. A request the caller sent
without credentials SHALL reach the target without credentials.

#### Scenario: Anonymous caller stays anonymous
- **WHEN** a caller with no `Authorization` header sends a request through a localhost router
- **THEN** the target receives no `Authorization` header and records the request as unowned

### Requirement: Router and serving nodes share the routing contract version
A router SHALL send its routing-contract version with every routed request. A target
that does not support that version SHALL refuse with a 409 naming both versions rather
than serve under a contract the router did not expect.

#### Scenario: Skewed router
- **WHEN** a router on contract version 2 routes to a target that supports only version 1
- **THEN** the target answers 409 naming versions 2 and 1, and the router's record shows it

### Requirement: Bounded use of a stale view
A routing node SHALL refresh its regional view on the view's TTL. If the fleet broker is
unreachable, it MAY keep routing on the last view until the view reaches the configured
maximum stale age; past it, it SHALL use only local and same-host options, as a node
without a fleet broker does. Each route record SHALL carry the view's age. A view SHALL
never authorise anything: identity, policy and capacity are checked at the target.

#### Scenario: Broker partition within bound
- **WHEN** the fleet broker becomes unreachable and the cached view is younger than the
  maximum stale age
- **THEN** requests keep being routed and their route records show the view age

#### Scenario: Broker partition beyond bound
- **WHEN** the cached view is older than the maximum stale age
- **THEN** the node stops routing across hosts and says so in the route record

### Requirement: Route records join across hops
Every routed request SHALL carry a request id (the caller's `x-request-id` if present,
else one minted by the first routing node), unchanged across hops. The routing node's
record SHALL name the vantage, view age, regions applied, candidates with reasons, the
chosen target and expectation, and any `route_stale` refusals; the target's demand record
SHALL carry the same id.

#### Scenario: Join entry and target records
- **WHEN** a request is routed from a `cn` router to a `cn` GPU node
- **THEN** the router's route record and the GPU node's demand record share one request id

### Requirement: One requirement evaluator
The facade, the fleet ranking and the planner SHALL evaluate requirements with one
implementation, including negation: `{"not": v}` is satisfied when the attribute is
declared and not equal to `v`; `{"not": [a, b]}` when it is declared and not any of them.
An undeclared attribute SHALL satisfy no clause, negated or not.

#### Scenario: Negation excludes
- **WHEN** a request requires `family: {"not": "gemma"}` and units of families `gemma`
  and `qwen` are resident
- **THEN** only the `qwen` unit can serve it

#### Scenario: Undeclared attribute fails negation
- **WHEN** a request requires `refusals: {"not": "stock"}` and no unit declares `refusals`
- **THEN** nothing satisfies it
