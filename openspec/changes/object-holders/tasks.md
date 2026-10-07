## 1. Catalog and holders (authority; additive schema)
- [ ] 1.1 `schema.sql`: `holders`, `holdings`; backfill one `authority` holding per existing blob. Tests: upgrade of a populated store keeps every object readable. Ledger: none (migration), logs count.
- [ ] 1.2 `holders.py`: registry, schema-validated config (unknown fields refuse), `authority` built in. Tests: validation, unknown holder refused. Ledger: registration events.
- [ ] 1.3 Claims: grant names a holder; holding recorded only on verified report; idempotent. Tests: wrong size, unissued digest, repeat claim, no bytes on authority. Ledger: every refusal.

## 2. Routing (pure)
- [ ] 2.1 `choose_holder` with locality tiers, fresh-measurement ranking, capacity, principal policy, reason string. Tests: synthetic fleets (same host, same segment, stale measurements, absent info → authority, excluded holder). Ledger: choice + candidates + reason.
- [ ] 2.2 Declared locality (`host_id`, `segment`) in node registration. Tests: absent never zero.

## 3. Reads and placement
- [ ] 3.1 Locator response and attempt-bound holder credential; holder verifies without authority secrets. Tests: wrong digest/holder/expired credential refused.
- [ ] 3.2 `holder` route kind on `RouteSet`; failover and resume across holders. Tests: reset mid-transfer on holder A continues on B.
- [ ] 3.3 Admission `inputs_unavailable`. Tests: only holder down → no attempt created.
- [ ] 3.4 Derive `locality_host` from input holders (preference only). Tests: nearer worker wins; full host falls through. Ledger: hint + source holder.

## 4. A reference holder and the first consumer's prerequisites
- [ ] 4.1 Reference holder process (`livestack_node.workloads.holder`): same PUT/GET wire, `GET /holder/status`, quota and retention; record in the design which of unchain `http-pull` / `storage-*` backs a remote holder. Tests: real sockets, injected faults.
- [ ] 4.2 Retention: authority prunes only what it holds; advertise references to others. Tests per requirement.
- [ ] 4.3 `GET /v1/holders` view and counters.

## 5. Docs and rollout (operator-run, separate)
- [ ] 5.1 `node-py/docs/object-holders.md`: protocol, config, operations, how to add a holder.
- [ ] 5.2 Add a bytes section to `_plans/fleet-broker.md` and routing-decision kinds to `_plans/decision-ledger.md`.
- [ ] 5.3 Rollout order: authority (additive), then holder on zz-tower2, then worker release; nothing switches for an app until its principal is allowed the holder. Rollback = remove that allowance.
