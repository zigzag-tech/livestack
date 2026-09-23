# fleetd — the dispatch loop as a weave, with Simple Jev as the first escalation rung

**Status:** DEPLOYED IN OBSERVE MODE, CANNOT YET SPEND — 2026-09-22. v1 was written by a Claude
session and critiqued by a Codex (gpt-6-astra) session (§7, kept verbatim); §§2–5 were
rewritten to fold that review in, and the design was then built.

Built: the durable lifecycle (`node-py/livestack_node/fleet_operations.py`,
`fleet_workers.py`, `fleet_pools.py`, `fleet_ops_api.py`, routes in `hostd.py`), the
supervision loop and the classifier rung (`fleetd/`). 122 tests green — 71 Python, 51
TypeScript.

Live on `xc-tower-ubuntu:8801` since 2026-09-22 02:36 CST, as release
`fleet-provisioning-6de47145` pinned by a systemd drop-in. The routes answer, the
operation store exists, the demand register is running, and the observe-only
property still holds (zero `[hostbroker] evict|warm` lines).

**Not yet true, and it matters:** `pools: []` — no broker on this fleet has
`LIVESTACK_FLEET_POOLS` set, and no provider credentials exist on the host at all,
so nothing has provisioned anything and nothing can. The classifier rung has no
qualification receipt (`fleetd/receipts/README.md` says what would produce one) and
runs only in `shadow`.

Both are the same kind of thing — handing a running process an authority it does not
yet have — and neither is an engineering decision. They are carried in
`openspec/changes/fleet-provisioning-activation/`, and this line becomes SHIPPED
(shadow) with the receipt's numbers when that lands.

OpenSpec: requirements are current truth in `openspec/specs/`
(`fleet-provisioning-operations`, `fleet-supervision-loop`,
`fleet-incident-classification`); the change that built them is archived at
`openspec/changes/archive/2026-09-22-fleet-provisioning-operations/`.

**Companions:** `fleet-broker.md` (phases 0–4 shipped), `fleet-scheduler.md` (§8 is
STALE: `schedule()` IS wired via `fleet_admit.py`), `decision-ledger.md` (SHIPPED as `ledger.py` +
`emit_*` in `hostbroker.py`; its own status line is stale), `~/jingway/docs/weaving.md`, `~/jingway/docs/decision-models.md`.

## 0. Ground truth, corrected

| Piece | State |
|---|---|
| `planner.py` `plan()` — what is resident where | SHIPPED, pure, driven by `hostd`/`hostbroker` |
| `fleet_scheduler.py` `schedule()` — run/queue/provision/burst | SHIPPED, pure; **wired** for `Admit`/`Queue` only via `POST /fleet/admit` → `fleet_admit.py` |
| `Provision` / `Deprovision` actions | emitted by `schedule()`, **never dispatched**. `fleet_dispatch.py` (89 lines) is test-only |
| Throughput model | missing — `_eta` = `est_duration_s`; why media-corpus 3b is left OFF (`fleet-broker.md` §5.4) |
| Decision ledger | SHIPPED — `ledger.py` (bounded JSONL), `emit_rank/admit/placement` in `hostbroker.py`; `emit_admit` does not yet record `lease_id`; no operation records |
| Phase 5 cross-host warm | gated on owner decision |
| Weaves (`routine()`/`weave()`, repair turns, handbacks, `RepairRecord`) | SHIPPED in jingway 2026-09-07 |
| Typed decisions (`server/decisions`, `HarmonyClassifierAdapter`, `defineDecisionRoutine`) | SHIPPED in jingway 2026-09-20; transport is Harmony `POST /v1/classifier` (Simple Jev v1, `livestack_node/decisions/simple_jev.py`) |

## 1. The one idea

Keep both brains pure and inference-free. Put the inference in the **loop around**
them — the thing that assembles state, dispatches actions, and notices when the world
did not do what the plan said. That loop is today an unstructured tick; it becomes a
weave. Green gate = zero tokens. A red gate climbs a ladder:

1. canned attempts / tier-1 (code)
2. **Simple Jev leaf** (Harmony resident 27B, `/v1/classifier`) — classifies the failure; code maps the class to a registered recovery workflow
3. full repair turn with typed handbacks, ending in `report_verdict`
4. `human_gate`

Jev enters only on unfamiliar territory. It is never in the hot path and never picks
*among targets* (that is `schedule()`'s job and the shape Jev measured badly on —
near-identical long strings, 87.5% order flips).

## 2. Where it lives

Two layers, two languages, one boundary that is explicit about identity:

- **Python (`livestack_node`) owns scheduling and the authoritative operation state.**
  `schedule()` stays pure. A new durable **operation store** records every
  provisioning operation (`intent → creating → created|rejected|uncertain →
  announced|failed → released`) with a stable operation id, idempotency key, provider
  request/instance ids, and the principal that authorised it. A versioned
  **planning/operation API** on `hostd` exposes it: `POST /fleet/plan` (a serialized
  `FleetPlan` over stable job ids, eligible pools, current reservations, effective
  policy — NOT the residency planner's `/plan` summary), `POST /fleet/operations`
  (claim-then-dispatch one proposed action; the claim is an atomic reservation
  against quota and pending creates), `GET /fleet/operations/{id}`.
- **TS (`livestack/fleetd/`, jingway-bound) owns the bounded supervision loop and the
  repair ladder.** It never computes placement and never holds capacity state; every
  action it takes is a claim against the Python store, carrying run/job/operation ids
  across the boundary.

Host brokers stay the sole residency authorities (`fleet-broker.md` §1); the fleet
view's soft-state promise is fine for an advisory view and not for a controller that
creates billable resources, which is why the operation store is durable and
reconciled on restart (owned provider instances + unfinished operations before any
new create).

Why TS for the loop: the weave, the decision leaf and the repair conversation are
useful there. Not because it avoids distributed-state work — explicit ids cross the
process boundary either way.

**Ephemeral vs reusable.** `fleet_dispatch.run_on_provider` is lease→run→release for
one workload; `Deprovision` is a no-op. This change picks **reusable fleet workers**:
a `Provision` creates an instance that announces itself (`announce.py`) and serves
jobs until deprovisioned. Ephemeral single-workload dispatch stays as it is and is
out of scope.

## 3. The weave

```
routine({
  id: 'fleet_tick',
  goal: 'Every queued job holds a claimed target, or a bounded operation is progressing toward one, under the effective policy.',
  policy: 'Never grant yourself a policy change (region, quota, budget). A create whose result is unknown is reconciled, never retried. Choose human_gate when the state contradicts itself.',
  handbacks: {
    read_fleet_view, read_operation(id), read_ledger_since(ts),
    reconcile_operation(id),                       // resolve 'uncertain' against the provider
    refresh_availability(provider, region, sku),   // scoped, capped cooldown; then re-plan
    schedule_wakeup(job_id, after_s),
    request_policy_change(kind, reason),           // opens a request; grants nothing
  },
  budget: { wallMs: 120_000, escalations: 1 },
}, async (step) => {
  const view = await step({ id: 'assemble_state', effect: READ, run: () => GET /fleet, gate: viewFreshnessPerNode });
  const plan = await step({ id: 'plan',           effect: READ, run: () => POST /fleet/plan, gate: planIsWellFormed });
  for (const a of plan.of(Provision)) {
    await step({
      id: `operation:${a.operation_id}`,
      effect: { class: 'irreversible', note: 'creates a billable instance' },   // resume derives from effect.class: gate_only + reobserve
      run: () => POST /fleet/operations { claim: a },
      reobserve: () => GET /fleet/operations/${a.operation_id},
      gate: operationAnnouncedOrTerminal,          // correlated receipt, not "a fresh node appeared"
    });
  }
  ...
});
```

Gates are correlated facts: the operation the step owns reached `announced` with the
node's own capability `ready`, or reached a terminal state with a structured reason.
"Some node became fresh" is not a green gate. Provisioning-in-progress and
policy-driven waiting are states with a wakeup, not red gates that invite inference
on every tick.

**Every deterministic recovery is a registered workflow** chosen by code from a
structured error class (`stage`, `class`, `code`): known codes bypass any model.
Deprovision requires a drain claim, zero active leases/jobs, no pending admission
and a final authoritative check — `schedule()`'s "no new job this pass" is not
idleness.

## 4. The Simple Jev leaf — a classifier, not a chooser

Simple Jev v1 on Harmony's resident 27B (`POST /v1/classifier`) is invoked only for
an incident whose error class is **unfamiliar** — no registered workflow matched.
It answers one question: **what kind of failure is this?**

```
questions.failure_class = { type: 'choice', instructions: '…', criteria: {
  capacity_shortage:         'The provider had no capacity for this pool at this time.',
  request_or_workload_fault: 'Our request or workload was wrong (spec, credentials, image, quota).',
  provider_fault:            'The provider misbehaved (5xx, timeout after accept, inconsistent state).',
  uncertain_effect:          'A create may have happened; the reply was lost or ambiguous.',
  needs_investigation:       'None of the above fits, or the evidence contradicts itself.',
}}
```

Each class maps to exactly one registered recovery workflow chosen by code:
`refresh_availability` + re-plan; investigate (credentials/spec/workload) with a
wakeup; provider cooldown (scoped, capped) + re-plan; `reconcile_operation`;
full repair turn (may `human_gate`). A one-token answer never selects a tier, never
provisions LAST_RESORT, never touches region or quota.

**Incident packet (`state`)** — versioned, bounded, the fields the ledger records:
stable job/operation/attempt ids; principal + delegated owner; absolute
created/deadline/observed times and snapshot ages; operation state and idempotency
key; provider request/instance ids; workload kind, need, selector, region
restrictions; eligible pools with concrete exclusion reasons; active + pending
reservations; owner AND prefix quota usage; structured error (stage/class/code) with
a bounded excerpt; attempt history; policy/weight versions; spend measured vs
estimated with currency, `unknown` never `0`. Required evidence is never trimmed —
oversized refuses. Counted with the served model's tokenizer against the compiled
prompt, not the raw evidence.

**Response** (`simple_jev.py`): `answers.failure_class = {type:'choice', choice,
confidence, probabilities}` + served model, `template_version`, usage. `confidence`
is normalised over the offered labels — a versioned, calibrated acceptance policy
decides, not an intuitive floor.

**Code has the last word:** acceptance policy → abstain; invariants → `violations`,
no selection, no runner-up; accepted → the mapped workflow runs; the outer weave
re-observes and verifies its own gate. Persisted together: evidence (or durable
reference) + digest, question/options/order, label mapping, task/profile/model/
template/order/acceptance versions, raw result, invariant feedback, executed
workflow, postcondition, eventual job/cost outcome — joined to the `RepairRecord`,
the Python operation id and the provider receipt.

**Modes.** `shadow` first: the leaf runs on captured incidents, side-effect free,
while the deterministic path handles them; `defineDecisionRoutine` returns a
`selection` even in shadow, so the application refuses shadow effects explicitly.
`serve` only after qualification on frozen, independently grouped cases with a
balanced permutation schedule and sealed holdout, measuring accepted-decision
correctness, dangerous-action errors, abstention/coverage, invariant rejections,
order disagreement and end-to-end cascade cost/p95 including fallback. Order
stability is necessary, not sufficient. No result flips itself to `serve`.

## 5. Delivery order

1. **livestack — deterministic lifecycle first.** Operation store + state machine,
   `POST /fleet/plan|operations`, atomic claim across admission/pending creates/
   owner+prefix quota, region enforced on the operation path, restart reconciliation,
   provider idempotency, reusable-worker adapter (Aliyun first), drain-gated
   deprovision. Extend the EXISTING ledger (`ledger.py`, `emit_*`) with operation
   records; record `lease_id`. Fake-provider tests: accepted-create/lost-reply,
   restart mid-create, concurrent ticks, workload-success/cleanup-failure, late
   completion after cancel, stale membership, busy-worker drain. One billed create
   per logical operation. Fix `fleet-scheduler.md` §8.
2. **`livestack/fleetd/` — bounded TS supervision.** The weave in §3 over the API in
   step 1; registered recovery workflows; no model in the loop yet.
3. **Simple Jev shadow.** `failure_class` profile in `shadow` on captured incidents;
   frozen case corpus; evaluation CLI; qualification receipt. `serve` is a separate,
   explicitly approved activation.
4. **Estimates.** Per-target runtime + cold-start from measured `/status` rates, and
   comparable resting pressure across device classes — `fleet-broker.md` §5.4 names
   both as blockers for media-corpus 3b; a throughput field alone resolves one.
5. Phase 5 cross-host warm — separate change, owner's call. benchday relay
   `/inventory` probe latency — independent, can go any time.

## 6. Open questions for the reviewer (v1 — answered in §7)

- Is putting the loop in TS (jingway) the right cut, or should escalation be an HTTP
  facade called from a Python loop in `hostd`?
- Is the five-candidate set right? What is missing, what should never be offered to a
  one-token classifier?
- Is `state` too thin / too fat for a 27B one-token decision? What would make its
  trace arguable a month later?
- Where does this go wrong silently (rule: absence and failure must not look alike)?
- Does anything here require a change to the jingway framework itself, or is it all
  application-level?

## 7. Second opinion

_(Codex gpt-6-astra to append here.)_

### Review — 2026-09-22

**Verdict: keep the pure schedulers and bounded repair idea; revise the execution
contract before implementing this plan.** A weave can supervise provisioning, but
the draft understates the missing control plane and overstates what the existing
dispatcher, ledger, and decision leaf guarantee. Jev is a plausible experiment
for unfamiliar failure triage, not yet a qualified first rung for spending money.

Section 6 contains **five** questions, not six. Answers 1–5 below follow those
bullets; answer 6 addresses delivery order, the additional implementation question
implicit in §5. This review is grounded in the local source, not a live deployment
check. Python paths below are relative to `node-py/livestack_node/`; Jingway paths
are relative to `~/jingway/`.

### 7.1 Should the loop live in TS or Python?

**Use TS for the bounded orchestration/repair loop, with Python owning scheduling
and the authoritative operation state. Do not replace hostd's residency loop.**
That makes a Jingway service reasonable, but the proposed HTTP cut is incomplete.

- `hostd.py:656` exposes the **residency** planner's summary at `/plan`, not a
  serialized `FleetPlan`. Its reconcile loop calls `plan_and_apply([])`; it is not
  already a fleet job queue/dispatch loop waiting to be wrapped.
- `fleet_admit.py:44,153` creates only `Tier.LOCAL`, running, non-elastic targets
  with zero cost, then schedules one newly synthesized job. There is no elastic
  pool catalog, persisted pending job, spend input, or preserved original deadline
  on this path. A refusal returns an answer; it does not enqueue work. Calling it
  again resets `created_at` and generates another job id.
- TS cannot call Python `schedule(FleetState(view))` through any of the listed
  endpoints. Add a versioned Python planning/operation API, with stable job IDs,
  original deadlines, eligible pools, current reservations, effective policy and
  typed actions. Do not port the scheduler into TS or parse its summary string.
  Revalidate/claim a proposed action atomically before dispatch; a read-only plan
  is not a capacity reservation.
- `fleet_dispatch.py:26,48` is a **lease → run workload → release** helper.
  `Admit` is `unroutable`; `Deprovision` is a no-op. It neither installs an
  announcing worker nor leaves a persistent target alive. Decide whether this
  feature runs ephemeral jobs or adds reusable fleet workers. Those require
  different completion gates and lifecycle adapters; two HTTP wrappers do not
  bridge the difference.

Keep host brokers as the sole residency authorities, as `fleet-broker.md` §1
requires. Add durable job/provisioning ownership separately: that document's
“soft state everywhere” promise works for an advisory fleet view, not for a
controller that creates billable resources. On restart, reconcile owned provider
instances and unfinished operations before issuing new creates. Use one fenced
writer/claim per operation, including across concurrent ticks or service replicas.

I disagree with rejecting the Python-loop/TS-facade alternative solely on trace
identity. A TS controller calling Python already crosses that process boundary;
explicit run/job/operation IDs must cross it either way. Choose TS because the
weave, decision leaf and repair conversation are useful there, not because it
removes distributed-state work. Bind project, user and conversation deliberately
(`docs/weaving.md` §0b), and avoid one fleet-wide repair conversation serializing
unrelated jobs (§3.2.5). A blocked provisioning operation must not stop admission
to healthy existing workers.

### 7.2 Is the five-candidate set right?

**No. It mixes diagnosis, immediate action, a persistent health change and an
escalation destination.** `mark_spot_unhealthy` does not say what happens to this
job next; `wait` lacks a duration/wakeup condition; and `human_gate` skips the
full repair rung that the draft says should precede it.

Prefer a short semantic **failure classification**, for example:
`capacity_shortage | request_or_workload_fault | provider_fault |
uncertain_effect | needs_investigation`. Deterministic, already-understood error
codes should bypass the classifier. Each class may select only a registered,
bounded recovery workflow: reconcile an uncertain create; wait with a wakeup;
investigate credentials/spec/workload failure; or refresh availability and
re-run `schedule()`. `needs_investigation` routes to full repair, which may block
for a human. Freeze and evaluate this task before treating those labels as final.

Do not let a one-token answer choose `provision_last_resort` directly. The
scheduler's hard rule is **no LAST_RESORT while any cheaper candidate is feasible**
(`fleet_scheduler.py:445`), and two SPOT failures do not establish that ONDEMAND
or LOCAL is infeasible. A verified, scoped availability update followed by a new
schedule preserves that rule. A transient capacity miss in one region/SKU also
does not justify marking an entire provider unhealthy. Give cooldowns fixed caps,
scope, expiry and recovery probes; code chooses these parameters.

**Remove `relax_region_policy(job)` from the automatic handbacks, including full
repair.** Region is caller/operator authority, applied before scheduling in
`hostd.py:547`; `fleet-broker.md` §§4.2–4.3 makes it a hard filter. A model may
request a separately authorized policy change, not grant itself one. Likewise,
do not offer quota overrides, cancellation of unrelated jobs, arbitrary workload
re-execution, or destructive deprovisioning without independently checked bounds.

Two policy claims need explicit decisions rather than prompt text:

- `Ledger` and `resolve_weights()` define a **soft** budget. The off-peak,
  over-budget LAST_RESORT prohibition is a new hard policy, not an existing
  invariant. If adopted, enforce it on all dispatch paths, including green
  deterministic ones; otherwise Jev alone is held to a rule the scheduler bypasses.
  `schedule()` does not call `resolve_weights()` itself.
- The current objective is a weighted tradeoff among feasible targets, with a
  special LAST_RESORT exclusion, not “the cheapest tier that meets the deadline.”
  `_score()` can favor speed/locality over cost among the remaining tiers. Align
  the routine goal with that contract or explicitly propose changing the policy.

### 7.3 Is the state suitable, and can its trace be argued a month later?

**It is too thin on decisive facts and potentially too fat on undifferentiated
ledger prose.** Model size and one output token do not fix missing evidence.
Use a versioned, bounded incident packet with required facts and optional
whole evidence items, matching `DecisionRequest`/`EvidenceItem`
(`src/server/decisions/contracts.ts:80,124`). Include:

- Stable job/operation/attempt IDs; authenticated principal and delegated owner;
  absolute creation/deadline/observation times; snapshot ages; current operation
  state; idempotency key; provider request/instance IDs. Distinguish “create
  definitely rejected” from “request timed out; effect unknown.”
- Workload kind, resource need, selector, scope and region restrictions; eligible
  pools and concrete exclusion reasons; estimates with provenance/uncertainty;
  active and pending reservations, instance limits, and both owner and enclosing
  prefix quota usage (`over_quota`, `fleet_scheduler.py:346`). A scalar
  `owner_quota_left` hides the aggregate ceiling.
- Structured error stage/class/code and bounded diagnostic excerpts; relevant
  attempt history; effective policy/weight versions; provider pricing currency
  and billing basis; measured versus estimated spend and pending commitments.
  Missing spend or ETA is unknown, never zero. Per-tier averages can hide the
  only compatible pool; retain evidence for the candidates actually considered.

Use the real tokenizer with room for the compiler's system preamble, repeated
question/options and evidence wrapper. `preflightDecision()` counts required
evidence/question text; that check alone is not a count of the final compiled
prompt. Refuse oversized required evidence rather than silently trimming it.
Make omission of optional history explicit. The Harmony adapter wraps evidence
items into `state`; application code should not duplicate the Simple Jev prompt.

The wire shape in §4 is shorthand, not exact: `simple_jev.py:133,162` returns
`answers[qid] = {type: 'choice', choice, confidence, probabilities}`, plus model,
template and aggregate usage. `confidence` is normalized over the offered
labels, not P(correct). A threshold needs a versioned, calibrated acceptance
policy; do not invent an intuitive floor and call it confidence calibration.

Persist the exact evidence or durable access-controlled references, its digest,
question/descriptions and order, label mapping, task/profile/model/template/order
and acceptance versions, raw result, invariant feedback, executed operation,
postcondition and eventual job/cost outcome. Join the decision trace,
`RepairRecord`, Python decision ID and provider receipt. Preserve unobserved
serving revision/tokenizer/quantization/cold-admission/cost as missing.

There is already a decision ledger: `hostd.py:825` constructs it;
`hostbroker.py:1213,1254,1273` emits rank/admit/placement records;
`ledger.py:191` supplies bounded JSONL storage. The draft's “DESIGN only/no
emitter” claims are stale. Extend that ledger and add durable operation records,
do not create a competing decision history. Existing rotation does not promise a
month of retention. Also, `emit_admit()` currently accepts but does not record
`lease_id`; cross-system correlation is work still to do.

### 7.4 Where can this fail silently?

**The largest risks are mistaking uncertain effects for failures and treating
observations as committed ownership.** These are prerequisites for paid dispatch:

1. **Duplicate spending after timeout/restart.** `gate_only` protects one block
   execution, not a future tick or another daemon. Persist an operation intent
   before create, use provider idempotency where supported, and reconcile ambiguous
   results before retrying. Carry fencing/identity to the Python write boundary.
   Cancellation of a TS request does not prove a remote create stopped. Bound RPCs
   and retain cleanup/reconciliation ownership after the block's timeout.
2. **Workload failure mislabeled as capacity shortage.** `run_on_provider()`
   catches every exception from the workload and lease cleanup, tries another
   offer, then raises `CapacityError`. A completed workload whose cleanup fails
   can be run again. Separate acquire, workload, result commit and release
   outcomes before allowing either canned retries or Jev escalation. Define
   what “SPOT failed twice” counts; the current helper defaults to five offers.
3. **Deleting a busy worker.** `schedule()` deprovisions eligible old elastic
   targets that received no *new* job this pass and cannot serve queued demand.
   It has no independent active-job/drain proof; an empty incoming job list is
   not evidence that an existing worker is idle. Before making the current no-op
   real, require a drain claim, zero active leases/jobs, no pending admission,
   and a final authoritative check.
4. **Quota races and lost ownership.** In `hostd.py:592–650`, usage is read,
   scheduling happens, then checkout happens separately; checkout failure is
   logged while the grant survives. `hostbroker.py:326,359` locks those individual
   operations but keeps leases in memory. “Never exceed quota” needs an atomic
   reservation across admission and provisioning, including pending creates and
   prefix quotas, with restart recovery. Repeating it in an invariant is not enough.
5. **False-green gates or permanent repair storms.** `/fleet` has no durable
   job queue to observe leaving. A fresh, ready node is not proof that this
   operation created it, nor that a particular job was dispatched/completed.
   Conversely, a successful ephemeral workload may never announce. Gate on the
   chosen lifecycle's correlated receipt and workload capability; use per-node
   snapshot/capability freshness, not only the view's new `generated_at`.
   Expected provisioning-in-progress or policy-driven waiting is a state with a
   wakeup, not an unfamiliar red gate needing inference on every tick.
6. **Unknown capacity treated as spare capacity.** `targets_from_view()` credits
   missing `in_flight` as zero used, hence four free slots by default; it records
   the uncertainty, but a “certified view” wrapper must not erase it. Preserve
   unavailable/stale/unknown/not-ready/saturated/policy-excluded distinctions.
   `_eta()` adds cold provisioning latency to a target-independent runtime;
   neither readiness nor model confidence proves the deadline can be met.
7. **Shadow actions and misleading traces.** `defineDecisionRoutine()` returns
   `selection` for an answered result even in `shadow` mode. The application must
   explicitly prohibit shadow effects. It also builds `trace` before applying
   `invariants`; rejection comes separately as `semanticFeedback`, while the
   result may remain `answered`. Persist that feedback and the final disposition,
   and consume only `selection` after all checks, never the raw answer.
8. **Missing evidence read as success.** Decision outcomes distinguish
   `unavailable`, `invalid_output`, `abstained`, `cancelled` and
   `deadline_exceeded`; keep their distinct reasons and fallback outcomes.
   Ledger writes and weave record writes can fail without failing the action.
   Surface that observability loss; an audit JSONL is not the transactional
   operation store. The classifier also shares the fleet's LLM capacity: an
   outage or saturation can disable both Jev and local full repair. Bound that
   cascade and produce a durable human block without recursive provisioning.

### 7.5 Does this need Jingway framework changes?

**Not necessarily. Start with application composition, but the sketch does not
plug into the current contracts as written.**

The current `src/common/routines/weave.ts:54` requires step goals and effects,
derives resume behavior from the effect class, and declares `onResume?: never`.
`src/server/weave/weave.ts:282` rejects an explicit `onResume` at runtime.
`src/common/routines/effects.ts:172` excludes `resume` for irreversible effects.
Parts of `docs/weaving.md` still describe the earlier explicit-resume API; follow
the types/implementation here. Keep `reobserve` for irreversible steps, supply
typed input/output schemas and effect declarations on every handback, and use
`carry_through` only when fresh evidence satisfies the gate. Do not label a paid
create compensatable merely because deleting the instance stops future charges.

There is no built-in Jev rung between `tier1` and full repair. `tier1` is documented
as mechanical and zero-token. One application-level implementation is a composed
`WeaveHost.escalate`: invoke a declared decision child once for eligible incidents,
route an accepted choice through the bounded effect surface, and return a valid
outcome or delegate to the conversation repair host. Keep shared deadlines,
abort signals, inference accounting, handback traces and recording intact. Specify
whether Jev and full repair consume one composed escalation or separate rungs;
the default step budget is one escalation, even when the block budget is two.
The outer weave still re-observes and verifies the gate.

Alternatively, use explicit child routines and effect steps in the application.
Either approach needs wiring and tests; `defineDecisionRoutine()` merely returns
a result/trace/selection or feedback. It neither executes the selected handback
nor automatically opens repair or persists everything. A reusable framework rung
may become worthwhile for several consumers, but is not a prerequisite for this
one. Python operation persistence and HTTP contracts are application work.

### 7.6 Is the delivery/qualification order right? (Additional question)

**No: establish deterministic lifecycle correctness before model-controlled
spending, and treat order stability as necessary but insufficient.**

1. Specify ephemeral-job versus reusable-worker semantics; define the operation
   state machine, stable IDs, pool catalog, Python planning API, atomic claims,
   auth/region/quota enforcement, restart reconciliation and durable receipts.
   Extend existing ledger emission. Keep hard budget changes explicit.
2. Exercise the deterministic path with fake providers: accepted-create/lost-reply,
   process restart, concurrent ticks/admissions, workload-success/cleanup-failure,
   late completion after cancellation, stale membership and busy-worker drain.
   Check one billed create per logical operation and no duplicate workload effects.
   This review itself makes no code changes or runtime-test claims.
3. Add bounded TS weave supervision, then a side-effect-free Jev shadow child on
   captured incidents. Shadow failure must not delay the incumbent recovery path.
   Compare Jev against deterministic handling plus ordinary repair/chat, not an
   assumption that one output token is cheaper.
4. Qualify the exact task/profile/policy on frozen, independently grouped cases
   with balanced candidate orders and sealed holdout. Measure accepted-decision
   correctness, dangerous-action errors, abstention/coverage, invariant rejections,
   order disagreement and end-to-end cascade cost/p95 latency including fallback.
   `decision-models.md` reports **10%** instability even for four distinct triage
   labels, and 2.97× input tokens/36% more median latency than ordinary chat on that
   task. Its 87.5% result concerns subtitle-cut strings, not measured fleet target
   selection. Neither result qualifies this new task. Passing `orderStabilityReport`
   alone could bless a classifier that is consistently wrong. Populate the exact
   `taskId@taskVersion` qualification scope and use the documented activation
   approval; a shadow result must not automatically flip itself to `serve`.
5. Move credible per-target runtime/cold-start estimates ahead of any production
   claim to meet deadlines economically; shadow plumbing can precede them.
   `fleet-broker.md` §5.4 names **two** blockers for media-corpus: missing throughput
   and incomparable resting pressure across device classes. A throughput field
   alone does not resolve the second. Keep cross-host warm as its separate owner
   decision. Relay inventory latency is independently useful and need not wait
   behind the provisioning/classifier work.
