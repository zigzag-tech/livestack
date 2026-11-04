## Purpose

Publish bounded, authenticated fleet-ranking snapshots through the native Harmony stream so the Hub can use current per-vantage rankings without polling the fleet broker.

## ADDED Requirements

### Requirement: Livestack publishes bounded per-vantage fleet rankings

The fleet broker SHALL publish `harmony.fleet-rank/1` through its authenticated Benchday daemon ingress. Each configured ranking SHALL be keyed by `(kind, region, vantage)` so distinct vantages for the same kind and region remain independent. The producer SHALL allow at most 32 configured keys and 64 targets per ranking, SHALL keep each body within 28 KiB, and SHALL use a 60-second TTL. Its default refresh interval SHALL be 30 seconds and any configured interval SHALL be bounded to 5 through 60 seconds. A present body SHALL include the key, `state: "present"`, `generated_at`, `ttl_s`, a stable content-derived `decision_id`, a reason of at most 256 characters, ordered targets, and preferences. Replaying identical content SHALL preserve the `decision_id`; changing ranking content at the same timestamp SHALL change it.

#### Scenario: Two vantages publish independent rankings
- **WHEN** the fleet broker is configured with two vantages for the same kind and region
- **THEN** it publishes two complete snapshots whose subjects and body keys each identify their own vantage

#### Scenario: Configuration exceeds the key bound
- **WHEN** more than 32 valid ranking keys are configured
- **THEN** the producer reports `fleet_rank_key_limit`, keeps the first 32 valid keys, and refuses every excess key without changing those retained keys

#### Scenario: A ranking body exceeds its bound
- **WHEN** a ranking has too many targets or its encoded body exceeds 28 KiB
- **THEN** the producer publishes a named failed partial for that key and does not publish a truncated success

#### Scenario: Decision identity is stable and distinguishes same-time content
- **WHEN** identical ranking content is replayed, or different content is generated at the same timestamp
- **THEN** identical content has the same `decision_id` and different content has a different `decision_id`

### Requirement: Ranking snapshots replace prior state and name removals

Each successful publication SHALL carry the complete ordered target snapshot for its `(kind, region, vantage)` key. A successful refresh SHALL replace the previous snapshot for that key as a whole. If the ranker returns no eligible targets, the producer SHALL publish an `ok` partial with `state: "removed"` and `removed_reason: "no_targets"`; it SHALL NOT encode an empty success or represent a computation failure as removal. Failures SHALL use the stream's `failed` status and a named error.

#### Scenario: A shorter ranking replaces the prior snapshot
- **WHEN** a new ranking has fewer targets than the previous ranking for the same key
- **THEN** the new complete target list is published and no prior target is merged into it

#### Scenario: No eligible targets removes the key
- **WHEN** the ranker successfully finds no eligible target for one configured key
- **THEN** the producer publishes an `ok` removal body for that exact key while leaving other keys unchanged

#### Scenario: Ranking computation fails
- **WHEN** the ranker or body validation fails
- **THEN** the producer publishes a named failed partial and does not send a successful empty or removed body

### Requirement: Authority refusal prevents fleet-ranking publication

The fleet-ranking source SHALL publish only through the authenticated daemon ingress and an accepted Hub authority registration. A registration refusal or lost authority SHALL remain a named unavailable/refused outcome and SHALL NOT be treated as a successful registration or produce accepted ranking state. The producer SHALL NOT use an HTTP fallback to bypass the stream authority.

#### Scenario: Authenticated authority registration succeeds
- **WHEN** the configured source registers through the authenticated daemon ingress and Hub accepts its authority
- **THEN** the producer may publish bounded fleet-ranking snapshots through that session

#### Scenario: The authority is refused or disconnected
- **WHEN** registration is refused, the authority is lost, or the ingress is disconnected
- **THEN** the producer records the named failure and publishes no ranking through an alternate path
