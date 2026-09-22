## 1. Deterministic lifecycle (Python) — before any model touches spending

Done 2026-09-22. `fleet_operations.py` (lifecycle + store), `fleet_workers.py`
(the provider seam + the Aliyun ECS adapter), `fleet_pools.py` (elastic pools as
operator config), `fleet_ops_api.py` (plan/claim/observe), wired in `hostd.py`.
Tests: `test_fleet_operations.py`, `test_fleet_workers.py`, `test_fleet_pools.py`,
`test_fleet_ops_api.py` — 71 tests, all green; the suite's 52 pre-existing
failures (a starlette/httpx `TestClient` incompatibility in this environment) are
unchanged from `main`.

- [x] 1.1 `fleet_operations.py`: state machine + SQLite store (bounded by count and age, enforcer named) with `operation_id`, `idempotency_key`, principal/owner, provider ids, structured error. Tests: every legal transition, every illegal one refused. Ledger: `operation.transition` record per transition.
- [x] 1.2 Atomic claim: owner + prefix `over_quota`, pending creates, admission usage, region — under one writer lock. Tests: concurrent claims, quota boundary, region-outside refused. Ledger: `operation.claim` accepted/refused with reason.
- [x] 1.3 Restart reconciliation: on startup and on `uncertain`, query provider by idempotency key; resolve to `created`/`rejected`; never re-create. Tests (fake provider): accepted-create/lost-reply, restart mid-create, late completion after cancel — one billed create per logical operation.
- [x] 1.4 `POST /fleet/plan` (serialized `FleetPlan`, stable ids, pools with exclusion reasons, reservations, policy version; uncertainty preserved). Tests: plan is a pure read, reserves nothing; unknown `in_flight` surfaces as `uncertainty`.
- [x] 1.5 `POST /fleet/operations` claim-then-dispatch, `GET /fleet/operations/{id}`; `409` on stale plan version. Same principals as `/fleet/admit`. Tests: auth refusals ledgered with source/principal.
- [x] 1.6 Reusable-worker adapter (Aliyun first): create with `operation_id` in the instance's announce env; `announced` only on the correlated receipt + `ready`. Tests: a fresh node without the id does not green the operation.
- [x] 1.7 Drain-gated deprovision: drain claim, zero leases/jobs, no pending admission, final re-check. Tests: busy-worker drain refused; `schedule()`'s `Deprovision` alone never releases.
- [x] 1.8 Ledger: `emit_admit` records `lease_id`; operation records joinable to admit/placement records by `job_id`. Tests: one query reconstructs an operation's history end to end.
- [x] 1.9 Docs: `_plans/fleet-scheduler.md` §8 and `_plans/decision-ledger.md` status corrected; `HARMONY.md` Fleet section gains the operation API; storage-bounds row for the operation store.

## 2. Supervision loop (`livestack/fleetd/`, TS)

Done 2026-09-22. `fleetd/` is a new workspace package. **Departure from 2.1:**
the loop takes a `hostFor(jobId)` factory rather than calling jingway's
`runtime.bind` itself — binding a project/user is the caller's decision, and a
library that bound one would decide it for every embedder. The property 2.1
wanted is asserted directly instead: an unreadable view makes no plan call and a
malformed plan dispatches nothing. Also split per the `fleet-supervision-loop`
spec: one read-only block for `assemble_state`/`plan`, then one block per acting
job so a blocked operation never serialises an unrelated one (`fleetd/README.md`
records why). Tests: `fleetd/src/tick.test.ts` — 21, all green; `tsc --noEmit`
clean.

- [x] 2.1 Package skeleton with jingway dependency; `runtime.bind` per tick with a conversation per job. Tests: `UnboundRoutineContextError` refused before any HTTP call.
- [x] 2.2 The `fleet_tick` weave: `assemble_state` → `plan` → `operation:<id>` steps (effect `irreversible`, `reobserve` from `GET /fleet/operations/{id}`, correlated gates). Tests against a fake `hostd`: green path issues zero escalations and zero tokens.
- [x] 2.3 Registered workflow table keyed by `{stage, class, code}`: `reconcile_operation`, `refresh_availability` (capped, scoped, expiring), `schedule_wakeup`, `investigate`, `request_policy_change`. Tests: a known code never opens an escalation; waiting-with-wakeup is not a red gate.
- [x] 2.4 Handbacks with typed input/output/effect; no region, quota or budget mutation exists on the surface. Test: the handback registry is asserted closed.
- [x] 2.5 Ledger: every step outcome and every escalation joined to `operation_id`; `RepairRecord` carries `operation_id` and `run_id`.

## 3. Simple Jev classification — shadow

- [ ] 3.1 `DecisionProfile` for `failure_class` via `HarmonyClassifierAdapter`; incident packet schema versioned; tokenizer count against the compiled prompt; oversized required evidence refuses. Tests: refusal paths, no credential in traces.
- [ ] 3.2 Composed `WeaveHost.escalate`: unmatched incident → one decision child → code maps class → workflow, or full repair → `human_gate`. Tests: shadow mode produces a `selection` and the app refuses to act on it; `unavailable` falls to a durable human block with no provisioning.
- [ ] 3.3 Acceptance policy versioned; invariants return `violations` with no selection; persisted joined record (evidence digest, order, label map, versions, raw result, feedback, executed workflow, postcondition, outcome). Tests: reversed candidate order yields a distinct permutation identity, same candidate ids.
- [ ] 3.4 Captured-incident corpus from the fake-provider suite + real ledger; frozen, independently grouped cases; balanced permutation schedule; sealed holdout. Evaluation CLI reports accepted-decision correctness, dangerous-action errors, abstention/coverage, invariant rejections, order disagreement, cascade cost/p95 incl. fallback.
- [ ] 3.5 Qualification receipt under `openspec/changes/fleet-provisioning-operations/receipts/`. `serve` activation is NOT a task here — it is a separately approved follow-up.

## 4. Verification before archive

- [ ] 4.1 Fake-provider suite green: one create per operation across lost-reply, restart, concurrency, cleanup failure, late completion, stale membership, drain.
- [ ] 4.2 Deployed on the fleet broker host in observe+shadow: operations flow, ledger joins, zero model effects. Evidence recorded in the change.
- [ ] 4.3 `openspec validate --specs` green; `_plans/fleetd-weave-jev.md` status updated to SHIPPED (shadow).
