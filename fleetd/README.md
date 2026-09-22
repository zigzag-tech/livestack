# `@livestack/fleetd` — the fleet supervision loop

One tick: read the fleet, ask the broker for a plan, and carry each action that
costs money through a jingway weave whose gates read **correlated facts**.

The loop supervises; it does not decide. Every placement comes from
`POST /fleet/plan`; every effect goes through `POST /fleet/operations`. It holds
no capacity state of its own, so restarting it costs nothing — the claim is
already on the broker's disk before the money is spent.

## The green path is free

Gates are deterministic. A tick whose operations reach `announced` inside their
deadline escalates zero times and spends zero tokens. A red gate consults a
registered workflow table keyed by the structured error `{stage, class, code}`
**before** any model sees it; only an incident nobody registered earns a repair
turn. And waiting is a state, not a failure: provisioning takes minutes, so an
operation inside its deadline schedules a wakeup rather than opening a
conversation about a machine that is booting exactly as expected.

## What a repair turn may do

`read_fleet_view`, `read_operation`, `read_ledger_since`, `reconcile_operation`,
`refresh_availability` (capped, scoped, expiring), `schedule_wakeup`,
`request_policy_change`. That is the whole surface, and a test asserts it is
closed.

There is no `provision`, no `choose_tier`, no `relax_region_policy`. Not caution
about models specifically: a surface that could select `LAST_RESORT` would
bypass the lexicographic guard in `fleet_scheduler.schedule()` that makes "last
resort" literal, and that guard is what stops a bad minute from becoming an
expensive hour. `request_policy_change` records a request for a person and
grants nothing.

## Setup

livestack has no jingway submodule, and this change is not the place to add one.
The fleet's convention is one working copy at `~/jingway`:

```bash
npm run link-jingway     # or: JINGWAY_PATH=/path/to/jingway npm run link-jingway
npm test                 # tsx --test src/*.test.ts
npm run typecheck
```

`link-jingway` REFUSES if jingway is not there rather than leaving an
unresolvable import to fail at the first production run. It also links jingway's
own `zod`: a `Handback`'s schemas are jingway `ZodType`s, and two zod instances
in one process produce schemas that are structurally identical and fail
`instanceof` — which surfaces as validation that silently never matches.

## Where the pieces are

| File | What it owns |
|---|---|
| `client.ts` | The broker's three routes, typed. A refusal is a value (`OperationRefused`), and a transport failure (`BrokerUnreachable`) is never flattened into one. |
| `gates.ts` | The deterministic floor. Correlated facts only. |
| `workflows.ts` | What code does about a failure it already understands. Nothing here spends. |
| `handbacks.ts` | The closed effect surface a repair turn may drive. |
| `tick.ts` | `fleetTick()` — the weave. |
| `observability.ts` | Joins jingway's repair records and step summaries to `operation_id`. |
| `incident.ts` | The versioned incident packet. Required evidence refuses rather than trims. |
| `classify.ts` | The Simple Jev rung: one `failure_class` question, code-owned invariants, the persisted record. |
| `escalation.ts` | The composed host that puts the classifier between the table and a full repair turn. |
| `corpus.ts` | Turning ledger incidents into frozen, grouped evaluation cases. |

## One departure from the design sketch

`_plans/fleetd-weave-jev.md` §3 draws one weave per tick with the operation steps
inside it. `openspec/changes/fleet-provisioning-operations/specs/fleet-supervision-loop/`
requires escalations to open in a conversation **per job**, so that a blocked
operation does not stop an unrelated job from being admitted. Both hold if the
tick is split where the ownership changes: one read-only block
(`assemble_state` → `plan`) over the whole fleet, then one block per acting job,
run concurrently, each with its own host. The gates and the effect surface are
identical either way; only the conversation boundary moved, and it moved to
where the spec put it.


## The classifier rung (shadow only)

An incident nobody registered a workflow for gets one bounded Choice question —
`failure_class` over `capacity_shortage | request_or_workload_fault |
provider_fault | uncertain_effect | needs_investigation` — on the model Harmony
already serves. **Code** maps the accepted class to exactly one registered
workflow. The model never names a tier, never provisions, never touches region,
quota or budget.

That split is not squeamishness. On four short, distinct triage labels this
transport shows ~10% order instability (jingway `docs/decision-models.md`). Ten
percent is usable for "which workflow should run"; it is not usable for "should
we rent the expensive thing" — and a one-token answer that could select
`LAST_RESORT` would bypass the guard that makes "last resort" literal.

Three refusals, each because the alternative is worse than no answer:

- **No runner-up.** A code invariant that rejects the winner returns
  `violations` and no selection. Taking second place is code overruling a model
  with a guess.
- **No acting in shadow.** The selection is recorded with its order, its
  probabilities and the workflow it *would* have run — and nothing happens.
- **No recursive provisioning.** An unreachable classifier is a durable human
  block. It runs on the fleet's own LLM capacity; bursting to restore it is a
  spending loop with an outage for a trigger.

Evaluation: `scripts/incident-corpus.mts` (capture + freeze, grouped by
operation), `scripts/observe-incidents.mts` (a balanced permutation schedule
against a live classifier), then jingway's `scripts/evaluate-decisions.ts` to
score. `receipts/README.md` says why there is no receipt yet and what would
produce one.
