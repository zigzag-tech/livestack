## ADDED Requirements

### Requirement: A speech request is placed by Harmony, not by the caller

The router SHALL route `POST /v1/audio/transcriptions` to a `polyasr` node chosen by `fleet_rank` from the router's own region vantage, with no placement input from the caller. The node kind SHALL be derived from the endpoint. The default `class=<kind>` requirement SHALL apply only to `llm` and `embed`.

#### Scenario: A speech node with no unit attributes is routable
- **WHEN** the fleet view lists a ready `polyasr` node whose units declare no attributes
- **THEN** the request is forwarded to it, byte for byte, with the routed-request contract header

### Requirement: A refused speech request fails over within a bounded budget

For a speech request the router SHALL treat a `503` received before any response byte as a refusal, record it as `busy: <reason>` when it carries busy advice (`device_busy` status, `Retry-After`, or "did not grant this device") and `unavailable: <reason>` otherwise, exclude that target, re-rank and retry, up to `HARMONY_ROUTER_SPEECH_ATTEMPTS` (default 3) attempts, replaying the same body. A busy target SHALL be dropped from the router's cached view until its next refresh. LLM requests SHALL keep their existing retry rules.

#### Scenario: The nearest node holds an image lease
- **WHEN** the top-ranked node answers `503 device_busy` and the second is ready
- **THEN** the caller receives the second node's `200` and the route record lists `busy: ...` then `ok`

#### Scenario: Budget is bounded
- **WHEN** five nodes are all busy
- **THEN** exactly three are tried and the caller receives the exhaustion response

### Requirement: Exhaustion names the reason and when to retry

When no provider can serve a speech request the router SHALL answer `503 {"error":"route_failed","attempts":[{"target","expect","result"}]}`, naming every attempt with the node's own reason, and SHALL set `Retry-After` to the shortest advertised busy window (clamped 1..300 s). A request for which no node of the kind exists SHALL answer `503` naming the kind and the ranking's reason.

#### Scenario: Every provider is busy
- **WHEN** two nodes answer busy with `Retry-After` 45 and 20
- **THEN** the response is `503`, `Retry-After: 20`, and both attempts carry the planner's reason

### Requirement: Placement is a versioned problem answered by a pluggable ranker

Placement SHALL be expressed as a `placement/v1` problem (kind, request, candidates with open-ended `facts` and an `eligible` stamp, open-ended `context`) and answered by a pure ranker selected per kind by configuration. Eligibility SHALL be decided before ranking and a ranker SHALL NOT make an ineligible candidate eligible. The deterministic default `default-v1` SHALL be the existing `fleet_rank` ordering. A ranker that raises, exceeds its time budget, does not accept the schema, is unregistered, or returns an illegal ranking SHALL be replaced by `default-v1` and the reason SHALL be recorded on the decision and in the log. Unknown fields SHALL be ignored by rankers and preserved in the log.

#### Scenario: A ranker resurrects an ineligible candidate
- **WHEN** a configured ranker returns a leased node
- **THEN** the decision uses `default-v1`, `fallback.why` says the id is ineligible, and the log row carries it

#### Scenario: Shadow ranker
- **WHEN** a shadow ranker is configured for a kind
- **THEN** both rankings and the observed outcome are logged and only the live ranker's choice is acted on

### Requirement: Every placement decision and its outcome is recorded in a bounded store

Each decision (problem as given, ranker name@version, ranking, shadow, chosen, fallback) and its outcome (success, cause, first-byte/first-partial/final latency, attempts; unknown is null) SHALL be written to a size-, file-count- and age-bounded log that is disabled when no age window is set and never blocks the request path.

#### Scenario: Offline replay
- **WHEN** logged problems are fed to a candidate ranker
- **THEN** the harness reports whether it ranked the target that actually succeeded first, counting illegal output separately
