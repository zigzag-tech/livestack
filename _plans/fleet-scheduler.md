# Fleet Scheduler — resource/budget/speed-aware admission & burst for Livestack

**Status:** design (this doc). Depends on `resource-planner.md` (the placement brain,
`node-py/livestack_node/planner.py`) — this is the layer *above* it.
**First consumer:** the Sorbonne MathVid client project (2026-07 delivery). Cloud
target committed to **Aliyun**. Sorbonne's needs enter purely as config/weights (BATCH
SLA class, ~5–10¥/video soft budget) — never as branches in `schedule()`. It is this
scheduler's first real driver: the client deadline funds the fleet layer, livestack
keeps the capability.
**Motivation:** the placement planner is resource-aware over devices that *already
exist* — it grants/defers/evicts on a fixed device set. It has no notion of **money**
or **deadlines**, and it cannot **create capacity**. Once we want "prefer local (it's
free), burst to cheap cloud under pressure, and only fall back to expensive serverless
(RunPod) as a last resort — with the balance shifting by time of day and cost
pressure," we need a second pure brain that decides the *set of targets* and *when to
pay for more*. That is the Fleet Scheduler.

---

## 1. The one idea (sibling to the placement planner)

> **`schedule(FleetState) -> FleetPlan` is a pure function**, exactly like
> `plan(WorldState) -> Plan`. It decides admission/routing/provisioning across a set
> of **targets** (running + provisionable); `plan()` then does residency *within* the
> targets it produces. Two brains, **one shared weight vector** — that is what makes
> it one system, not a bolt-on.

```
 schedule(FleetState) -> FleetPlan     [budget + speed dominate]  ← NEW
    ├─ Admit(request -> target)        run now on an existing target
    ├─ Queue(request, reason)          wait (local will free / off-peak)
    ├─ Provision(target_spec, n)       spin up cheap cloud / (last resort) RunPod
    ├─ Deprovision(target)             drain idle burst capacity (hysteresis)
    └─ (no Reject: budget is soft, see §4)
                    │ produced targets
                    ▼
 plan(WorldState) -> Plan              [resource dominates]  ← existing, unchanged
    Load / Evict / Grant / Defer
```

Transport-free, clock-injected, tested with fakes — same discipline as the planner.

## 2. Targets are cost/latency-annotated devices

Extend `Device` (or wrap it as `Target`) so the objective can reason about money and
time, not just fit:

```python
tier: Tier                  # LOCAL | SPOT | ONDEMAND | LAST_RESORT
cost: CostModel             # {per_hour} or {per_job}; LOCAL ≈ 0 (sunk cost)
provision_latency_s: float  # 0 local · ~90 Aliyun ECI · variable RunPod
elastic: bool               # can we spin up more of this tier?
```

Tiers (cost ascending): **LOCAL** (tower0 GPU, ≈0, fixed) < **SPOT** (Aliyun
ECI/spot, cheap, elastic) < **ONDEMAND** (Aliyun g8i, medium) < **LAST_RESORT**
(RunPod serverless, expensive, near-instant scale).

Concrete Aliyun provisioning primitives per elastic tier — all **per-job start/stop**,
driven by `Provision`/`Deprovision`, never Terraform: **SPOT/ONDEMAND** GPU jobs land
on **ECI-GPU** containers submitted as k8s `Job`s via **ASK** (serverless k8s) — the
pod exits, billing stops; short, spiky inference can instead use **FC-GPU** functions
(scale-to-zero, per-100ms). Terraform owns only the long-lived base (VPC, serving ECS,
OSS/CDN, the ASK cluster shell). `provision_latency_s` ≈ tens of seconds for ECI-GPU,
so INTERACTIVE prefers already-warm LOCAL while BATCH (Sorbonne teacher-upload)
absorbs the cold start.

Prefer-local and RunPod-last-resort are **not special cases** — they fall out of the
cost term. Local wins on budget; RunPod (top cost tier) only wins when speed is
weighted high *and* nothing cheaper can meet the deadline. One hard guard makes "last
resort" literal:

> **RunPod is only a candidate once every cheaper feasible tier is exhausted or
> provably cannot meet the request's deadline.** (Lexicographic, evaluated before the
> weighted score.)

## 3. Jobs carry speed intent (SLA class + default deadline)

Extend `Request` (it already has `created_at` for aging):

```python
sla_class: Sla             # INTERACTIVE | NORMAL | BATCH  — sets a *default* deadline
deadline: Optional[float]  # explicit; overrides the class default
est_duration_s: float      # to compute ETA per target
```

`ETA(target) = queue_wait + provision_latency_s + est_duration_s`. A target is
deadline-feasible iff `now + ETA <= deadline`. Sorbonne teacher-upload = **BATCH**
(async, show-progress, cost-first); a future live path = **INTERACTIVE**.

## 4. Budget is soft — a cost term + a pressure signal, never a gate

Budget **never blocks or rejects** work. It enters two ways:

1. **Objective term.** Score (minimize):
   `w_budget · cost(target) + w_speed · eta_norm(target) − w_resource · local_fit`
   subject to hard constraints = capability/selector match + deadline feasibility
   (+ the RunPod last-resort guard). Prefer-cheap is `w_budget`; prefer-fast is
   `w_speed`; prefer-local/keep-warm is `w_resource`.
2. **Advisory ledger → cost pressure.** Track spend per window (per-video soft target
   ~5–10¥ per Sorbonne AGENTS.md §2.6; rolling monthly total). Drift over target does
   **not** stop anything — it feeds the auto profile switch (§5), nudging weights
   toward budget-first. Cost bites gracefully instead of hard-failing.

Every dispatched job is tagged with `{target, tier, est_cost, actual_cost?}` on its
`ResultBundle` so "prefer-local" is measured, not asserted.

## 5. Weights vary over time — a layered `WeightPolicy`

Weights live on `PlannerPolicy` (already exists). A resolver composes three layers,
last-wins:

```
resolve_weights(now, fleet) =
    base    = time_of_day_profile(now)         # peak → speed-first; off-peak/nightly → budget-first
    auto    = adjust(base, pressure(fleet))    # queue depth · deadline-risk · cost-pressure (§4.2)
    final   = manual_override ?? auto           # explicit API/env pin wins outright
```

- **Time-of-day (base):** deterministic profiles. `peak`=speed, `offpeak`=budget,
  `balanced`=default. Covers "batch overnight, cheap."
- **Auto (adjust):** raise `w_speed` when the queue backs up or deadlines are at risk;
  raise `w_budget` when spend drifts over target; relax when idle. Self-tuning.
- **Manual (override):** pin a profile regardless — force budget mode during a cost
  crunch, or speed mode for a client demo.

## 6. Reuse the planner's proven anti-pathology machinery

The placement planner already solved the stability problems; mirror them:

- **Anti-starvation aging** (planner has it for priority) → **speed-aging**: a BATCH
  job's effective `w_speed` climbs as its soft deadline nears, so cheap-but-slow work
  still lands on time without a hard deadline miss.
- **Anti-thrash hysteresis** (planner's `min_residency_s` / `restore_debounce_s`) →
  **provisioning hysteresis**: a burst worker is kept warm `min_uptime_s` and drained
  with a debounce, so we don't flap cloud workers up/down (each flap costs real money +
  provision latency).

## 7. Scope

**MVP** (one pure module + tests): target tiering, deadline-feasibility + ETA,
weighted score, RunPod last-resort guard, soft cost ledger, two base profiles
(peak/offpeak) + manual override. `Provision`/`Deprovision` dispatch via the existing
`aliyun/` and `runpod/` adapters.

**Full:** continuous auto weight adjustment (queue/deadline/cost pressure),
speed-aging, provisioning hysteresis, spot-reclaim handling, per-project budgets.

## 8. Where it lives & how it's tested

- `node-py/livestack_node/fleet_scheduler.py` — the pure `schedule()` + dataclasses.
- `node-py/livestack_node/hostd.py` (or a thin `fleetd`) assembles `FleetState`
  (from broker `/status` + the ledger) and dispatches `FleetPlan` actions.
- `tests/test_fleet_scheduler.py` — mirror `test_planner.py`/`test_federation.py`:
  prefer-local when local idle; burst SPOT under queue pressure; RunPod *only* when a
  tight INTERACTIVE deadline can't be met cheaper; off-peak never provisions
  LAST_RESORT; manual override beats time-of-day; cost-pressure shifts weights.

## 9. Consumers

Sorbonne (`sorbonne-agent/docs/PLAN.md` §5.5/§10): the "prefer-local, burst-on-pressure
placement policy" gap *is* this scheduler. unchain routes each portable job through
`schedule()`; heavy GPU steps (`transcribe`/`tts`) still lease via Harmony within the
chosen target. Also serves any Livestack fleet consumer (meeting-digest batch,
interactive chipgen).
