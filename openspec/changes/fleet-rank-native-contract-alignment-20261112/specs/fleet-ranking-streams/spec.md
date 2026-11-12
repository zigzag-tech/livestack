## ADDED Requirements

### Requirement: Native rankings use one bounded realm-authoritative pair contract

The native producer SHALL publish harmony.fleet.rank/1 through authenticated
daemon ingress with one authority per realm and one configured vantage per
(kind, region). The subject SHALL contain kind and region, and the body SHALL
repeat them. The producer SHALL retain at most 32 active configured pairs and
SHALL refuse malformed, conflicting duplicate, or excess configuration with
named outcomes. Excess pairs SHALL NOT disable or replace the first 32.

#### Scenario: The 33rd pair cannot evict an existing pair

- **WHEN** configuration contains 33 valid distinct pairs
- **THEN** the first 32 remain active and the excess pair is refused by name

#### Scenario: Two vantages for one pair are refused

- **WHEN** configuration names a second vantage for an already configured pair
- **THEN** the duplicate has a named refusal and cannot replace the selected vantage

#### Scenario: A second realm authority cannot publish as a successful source

- **WHEN** another registered live authority already holds the contract
- **THEN** registration is refused by name and the producer does not fall back to HTTP

### Requirement: Ranking bodies preserve typed evidence within declared bounds

Every successful ranking body SHALL contain kind, region, vantage, generated_at,
ttl_s, decision_id, chosen, reason, targets and prefer. It SHALL validate against
the matching catalog schema, hold at most 64 targets and 8 preference clauses,
and encode to at most 28 KiB. TTL SHALL be positive and at most 600 seconds.
Attribute and metric preferences SHALL retain their existing typed vocabulary;
hard eligibility keys SHALL NOT replace preference clauses. Invalid and
over-limit results SHALL be named failures, never clipped successful values.

#### Scenario: A valid ASR or metric preference reaches the body unchanged

- **WHEN** the ranker returns a supported typed preference and receipt
- **THEN** both retain their meaning and satisfy the catalog schema

#### Scenario: An oversized result is refused

- **WHEN** a result exceeds a target, preference, field, nesting or byte bound
- **THEN** the producer emits a named failed partial and no clipped successful body

### Requirement: Decision IDs distinguish different decisions within one second

The producer SHALL derive a stable decision ID from the complete canonical
visible decision content. Identical content SHALL yield the same ID; different
content SHALL yield a different ID even within one second.

#### Scenario: Two same-second target orders differ

- **WHEN** target order changes while the integer generated second is unchanged
- **THEN** the decision IDs differ and the route ledger can distinguish the decisions

### Requirement: No-target absence and source failure are distinct

A successful computation with no eligible targets SHALL publish a bodyless
absent partial for that pair. Reader errors, computation errors, invalid data,
and authority or ingress failures SHALL retain named failure outcomes. A
reconnect SHALL re-register the same native contract before publication.

#### Scenario: Empty eligible set withdraws the old ranking

- **WHEN** the ranker successfully returns no eligible targets
- **THEN** the producer publishes absent with no body or error for that pair

#### Scenario: Rank computation throws

- **WHEN** the ranker cannot compute a result
- **THEN** a named failed partial is emitted and absence is not fabricated

#### Scenario: Shadow acknowledgements never look like active publication

- **WHEN** ingress acknowledges a present, absent or failed ranking publication as shadowed
- **THEN** the runtime reports `fleet_rank_authority_refused:shadowed`
- **AND** none of those outcomes is claimed as an active authoritative publication
