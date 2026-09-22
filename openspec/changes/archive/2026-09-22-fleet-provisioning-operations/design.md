## Context

Two pure brains exist and stay pure. `hostd` already serves `/fleet`, `/fleet/rank`
and `/fleet/admit` (phases 0–4 of `_plans/fleet-broker.md`). What is missing is a
control plane for actions that cost money, and a bounded place for inference when
that control plane meets something unfamiliar. Full reasoning and the second
opinion this design answers: `_plans/fleetd-weave-jev.md`.

## Ownership of durable state

| State | Owner | Where |
|---|---|---|
| Placement decisions (`FleetPlan`) | Python, pure | `fleet_scheduler.schedule()` — unchanged |
| Provisioning operations (state machine, ids, receipts) | Python | new `fleet_operations.py`, SQLite under `LIVESTACK_LEDGER_DIR`, bounded by count + age |
| Reservations against quota and pending creates | Python | same store; one claim per operation, held under one writer lock |
| Membership / residency | host brokers | unchanged |
| Decision ledger records | Python | existing `ledger.py` (bounded JSONL); gains `operation.*` records |
| Weave run scope, `RepairRecord`, decision traces | TS (jingway) | jingway's own stores |
| Provider instances | the provider | reconciled into the operation store on startup and on `uncertain` |

Ids that cross the Python↔TS boundary, always explicitly: `job_id`, `operation_id`,
`attempt`, `run_id` (weave), `decision_id`. The TS loop holds no capacity state; a
restart of `fleetd` loses nothing that matters.

## Operation state machine

```
intent ──claim ok──▶ creating ──provider ack──▶ created ──announce+ready──▶ announced
   │                    │                         │                             │
   │ claim refused      │ reply lost / ambiguous  │ never announced (deadline)  │ drain claim +
   ▼                    ▼                         ▼                             │ zero leases +
rejected             uncertain ──reconcile──▶ created | rejected              ▼ final check
                                                                         released
```

- A claim is atomic across admission (`hostd` `/fleet/admit` usage), pending creates,
  and `over_quota` on both the owner and every enclosing prefix. Claim refusal is a
  terminal, ledgered answer; it never enqueues silently.
- `creating` is written BEFORE the provider call, with the idempotency key. A process
  that dies here comes back to an `uncertain` operation and reconciles it; it never
  re-creates.
- `uncertain` is a first-class state with its own ledger record. It is resolved only
  by `reconcile` (query the provider by idempotency key / name prefix), never by retry.
- `announced` requires the correlated receipt: the node that announced carries this
  `operation_id` (via the provision's cloud-init/env), and its capability reports
  `ready`. A fresh node without the id does not green this operation.
- `released` requires: drain claim recorded, zero active leases and admitted jobs on
  the node, no pending admission targeting it, and a final authoritative re-check
  under the writer lock. `schedule()`'s `Deprovision` is a proposal, never a proof.
- Region: the operation carries the caller's region policy and the provider adapter
  refuses a zone outside it. The same filter `hostd.py` applies before scheduling
  applies again at dispatch.

## API (versioned, `v1`)

- `POST /fleet/plan` → `{plan_version, jobs:[{job_id, created_at, deadline_at, owner,
  principal, sla, need, selector, regions}], pools:[…eligible with exclusion reasons…],
  reservations:[…], policy:{weights_version, …}, actions:[Admit|Queue|Provision|
  Deprovision with stable ids]}`. It is a read; it reserves nothing.
- `POST /fleet/operations` `{action, plan_version, idempotency_key}` → claim then
  dispatch. `409` when the plan version is stale or the claim fails, with the reason.
- `GET /fleet/operations/{id}` → the state, receipts, structured error
  `{stage, class, code, excerpt}`.

Auth: the same principals as `/fleet/admit`; a delegating principal's asserted owner
is recorded as such.

## The supervision loop (`fleetd`, TS)

A jingway `routine()` per tick, bound to a fixed project/user and a conversation
per **job**, not one fleet-wide conversation (weaving.md §3.2.5 — unrelated jobs must
not serialise on one repair thread). Steps: `assemble_state` → `plan` → one
`operation:<id>` step per `Provision`/`Deprovision`, effect class `irreversible`
(resume is derived: gate-only with `reobserve`). Gates read correlated facts from
`GET /fleet/operations/{id}`.

A red gate first consults the **registered workflow table** keyed by structured error
class: known codes never reach a model. Workflows: `reconcile_operation`,
`refresh_availability(provider, region, sku)` with a capped, scoped, expiring
cooldown followed by re-plan, `schedule_wakeup`, `investigate` (credentials / spec /
image / quota with a wakeup), `request_policy_change` (opens a request; grants
nothing). Waiting-with-wakeup is a state, not a red gate.

## Simple Jev rung — classification only

For an incident with no matching workflow, an application-level composed
`WeaveHost.escalate` runs one `defineDecisionRoutine` child: question
`failure_class` over `capacity_shortage | request_or_workload_fault | provider_fault
| uncertain_effect | needs_investigation`. The incident packet is the versioned
`state` in `_plans/fleetd-weave-jev.md` §4, counted with the served tokenizer against
the compiled prompt; required evidence refuses rather than trims.

Code maps class → workflow; `needs_investigation` → full repair turn → `human_gate`.
A one-token answer never selects a tier, never produces `provision_last_resort`,
never touches region, quota or budget. Acceptance is a versioned calibrated policy
over `confidence` (normalised over offered labels); invariants return `violations`
with no selection. Shadow mode refuses effects explicitly (the leaf returns a
`selection` even in shadow). Everything listed in §4 "persisted together" is
written, joined to the `RepairRecord`, the operation id and the provider receipt.

Budget: one composed escalation per step (Jev + full repair count as one), block
budget one escalation per tick per job; the classifier shares the fleet's LLM
capacity, so classifier `unavailable` falls straight to a durable human block
without recursive provisioning.

## Decisions to make explicit (not hidden in prompt text)

1. **Hard budget rule?** Today `Ledger` is a soft term in `resolve_weights()`. This
   change does NOT add "no LAST_RESORT off-peak over target" as a hard rule. If the
   owner wants it, it is a follow-up applied on every dispatch path, green ones
   included.
2. **Objective wording.** `schedule()` is a weighted trade among feasible targets
   with a LAST_RESORT exclusion, not "cheapest tier that meets the deadline". The
   routine goal is written to that contract.
3. **Provider first.** Aliyun on-demand/spot (`aliyun/` adapter) is the first
   reusable-worker adapter; RunPod stays reachable through the existing ephemeral
   helper until its adapter is written.

## Silent-failure controls (rule: absence ≠ failure)

- Unknown `in_flight`, spend, ETA or capability stays `unknown`; `targets_from_view`'s
  default-to-zero is surfaced in the plan as `uncertainty`, never erased by a wrapper.
- Ledger/record write failures are reported as `observability_degraded` on the
  operation and in `/fleet`; an audit JSONL is not the transactional store.
- Classifier outcomes `unavailable | invalid_output | abstained | cancelled |
  deadline_exceeded` keep distinct reasons and distinct fallbacks.
- Every state transition logs unconditionally with the condition in the message.

## Alternatives considered

- Python loop in `hostd` + TS escalation facade: viable; rejected because the weave,
  decision leaf and repair conversation are where the value is, and ids cross the
  boundary either way.
- Model chooses the next action directly (v1 of the plan): rejected — it mixes
  diagnosis with spending authority and lets one token bypass the LAST_RESORT guard.
- Ephemeral single-workload provisioning: kept as is; different lifecycle and gates.
