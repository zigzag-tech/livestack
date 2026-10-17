# Design: identity-facts source streams

## Source contracts and identity

The fleet broker registers and publishes only `livestack.identity-facts.fleet/1`; only the workload authority registers and publishes `livestack.identity-facts.workload/1`. Both contracts are host-service state contracts, account-scoped and one-per-realm. `StreamsRuntime` uses the normal native producer registration path, which requires the service's authenticated daemon ingress and Hub authority registration. A refusal or missing authority link remains a visible failure; a local Broker-only fixture is not evidence that the authority contract works.

Each process uses its explicit `LIVESTACK_IDENTITY_AUTHORITY_ID` (fleet) or configured `identity_authority_id` (workload) as the authority in its Facts. The transport `partial.source` is stamped by authenticated daemon ingress and is bound by Hub to the registered one-per-realm holder; it is not a Livestack hostname or operator-supplied daemon ID. Missing or invalid identity configuration disables that publisher with a named failure, and workload authority identity never falls back to the hostname. The fleet stream is attached only when `observe=True`; host brokers do not compete for the fleet contract.

## One complete cut, bounded pages

Each refresh reads the owning authority's current relations and calls its existing `IdentitySnapshotPublisher.snapshot()` once. The same publisher remains shared with the HTTP compatibility route during cutover, so HTTP and stream reads cannot create independent generations. The cut is serialized and checked before any page is published: at most 4,096 Facts, at most 2 MiB of canonical snapshot JSON, and at most 128 pages. Pages are greedily packed so the complete stream partial envelope stays at or below 24 KiB. Every page carries the same generation, sequence, observation time, page count, and its zero-based index; the empty cut is one empty page.

When a cut shrinks, the new `page_count` defines the only current indexes; trailing page keys from the earlier cut carry an older inner fence, are ignored by the Hub assembler, and expire under the 60-second stream TTL. A malformed or oversized source cut is rejected before publication. A mid-cut ingress failure is logged and retried on the next complete refresh; the Hub's inner-fence assembler will not accept that mixed generation/sequence. The heartbeat period is 15 seconds.

## Process lifecycle

For fleet hostd, create one publisher from the explicit authority ID and pass it to the observe-only stream runtime; each refresh invokes one pure relation-to-cut function. The workload authority passes `WorkloadServer.identity_facts` to the stream runtime after the server has bound its immutable principal table and recovered its state. `StreamsRuntime` owns the periodic loop and cancels it during `stop()` along with the producer. Publishing uses the existing serialized 40-frame/s ingress pace.

The old endpoints are removed after the Hub consumer is migrated and the repository search finds no other callers. There is no Hub fallback. Source endpoints are not part of the new contract and are not retained as an alternate data path.

## Verification and ledger

Unit tests cover byte-accurate packing, exact maximum and over-limit cuts, empty cuts, stable fences across pages, trailing-page removal, and rejection before any candidate page is sent. Producer tests cover fleet-only vs host-only registration, explicit authority ids, shared publisher sequence, retry after ingress loss, and stop behavior. The coordinated Benchday isolated assertion uses an authenticated Hub authority registration plus refusal and missing-link controls; it never targets production. These identity reads do not write decision-ledger entries.
