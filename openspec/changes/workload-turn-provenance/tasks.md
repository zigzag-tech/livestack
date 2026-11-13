## 1. Authority contract
- [x] 1.1 Add closed provenance validation and opt-in principal permission; HTTP tests reject worker/reader/ungranted caller, unknown fields, forged authority fields and foreign delegated owner. Ledger obligation: stamp actual submitting principal/effective owner.
- [x] 1.2 Persist immutable bounded provenance with compatible migration and idempotency conflict controls; real SQLite tests cover replay, conflicting turn and concurrent submissions. Ledger obligation: source identity cannot be relabeled.
- [x] 1.3 Add indexed exact-turn owner projection and authority explanation; real HTTP/SQLite tests cover sibling owner, sibling turn, pagination/ties, response cap, query plan and attempts. Ledger obligation: observed worker/state comes from authority tables.
- [x] 1.4 Enforce24h default/max7day metadata retention on reads and sweep including retained jobs; clock-controlled tests prove expiry without deleting job data. Ledger obligation: named TTL and enforcer documented.
## 2. Integration and release
- [ ] 2.1 Run focused authority tests with explicit access/bound/retention negative controls; run repository normal checks and open PR with precise evidence.
- [ ] 2.2 Coordinate operator rollout, then prove actual producer → authority → admitted worker → owner-scoped reader with Jingway/Benchday; no mocked acceptance or worker choice.
- [ ] 2.3 Update HARMONY/design/storage documentation, sync capability spec and archive after shipment and verified E9 journey.
