## 1. Contract Projection

- [x] 1.1 Align configured keys, present-body fields, content-derived decision ids, and per-target bounds; verify `test_configuration_is_bounded_and_cadence_is_named`, `test_existing_ranker_preserves_per_vantage_preference_and_target_order`, `test_decision_id_is_stable_for_identical_content_and_changes_with_same_second_content`, and overflow controls. Ledger: do not create a placement/outcome row; carry `decision_id` for the Hub manifest's existing route-ledger link.
- [x] 1.2 Publish each key as a complete snapshot, emit a bounded named removal for successful empty results, and preserve named failures; verify full-replacement, empty-rank, over-limit, and shadow-mode producer tests. Ledger: Hub records the consumed snapshot id at the request decision boundary; Livestack adds no durable state.

## 2. Authority and Integration

- [x] 2.1 Verify a refused `harmony.fleet-rank/1` registration remains named and emits no ranking; cover with `test_refused_fleet_rank_authority_stays_named_and_never_publishes`. Ledger: no ranking or route record is created from a refused authority.
- [ ] 2.2 Read the existing admitted Benchday checks `speech-capacity.daemon-pushed-ranking-reaches-manifest` and `speech-capacity.ranking-cannot-introduce-ungranted` to terminal without duplicating them; record their exact result/tree in the Benchday fleet-rank evidence. Ledger: verify the accepted decision id reaches the Hub manifest route record and cannot widen eligibility.

## 3. Design Record and Closeout

- [x] 3.1 Update `_plans/fleet-broker.md` §8.1 to replace the stale `(kind, region)` and endpoint-body assumptions with the authenticated `(kind, region, vantage)` stream contract; verify the section describes explicit removals and no HTTP fallback. Ledger: no new ledger storage is introduced.
- [x] 3.2 Run the full focused producer suite and `openspec validate` on the final source tree; record the exact tree and receipts in the commit body. Ledger: confirm the stream source id remains the Hub's correlation key and no unbounded producer state was added.
